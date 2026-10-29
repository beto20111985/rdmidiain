using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Channels;
using System.Threading.Tasks;
using Microsoft.Win32.SafeHandles;
using RdPlayer.Core;
using RdPlayer.Core.Config;
using RdPlayer.Core.Ipc;
using RdPlayer.Core.Logging;
using RdPlayer.Core.Update;
using RdPlayer.Core.Watchdog;

namespace RdPlayer.Watchdog;

/// <summary>
/// Supervisor da casca: lança/adota o RdPlayer.Shell, recebe heartbeats (named pipe, só usuário atual, cliente
/// verificado), recarrega o WebView quando o bundle para de progredir, reinicia o processo travado/morto com backoff,
/// aplica atualizações por troca de slots com probação e rollback, e registra tudo em log rotacionado.
/// </summary>
internal sealed class WatchdogService
{
    private abstract record Ev;
    private sealed record ClientConnected(int Pid) : Ev;
    private sealed record ClientDisconnected : Ev;
    private sealed record ClientMessage(WdMessage Msg) : Ev;
    private sealed record ShellExited(int Pid, int? Code) : Ev;

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetNamedPipeClientProcessId(SafePipeHandle pipe, out uint clientProcessId);

    private readonly CommandLine _args;
    private readonly PlayerPaths _paths;
    private readonly ConfigLoadResult _cfg;
    private readonly RotatingFileLog _log;
    private readonly string _selfExe;
    private readonly string _root;
    private readonly UpdateSlots _slots;
    private readonly WatchdogPolicy _policy;
    private readonly RestartBackoff _backoff = new();
    private readonly Channel<Ev> _events = Channel.CreateUnbounded<Ev>();
    private readonly CancellationTokenSource _cts = new();
    private readonly object _writeLock = new();
    private readonly string _pipeName;

    private Process? _shell;
    private int _shellPid;
    private long _shellStartedMs;
    private long? _nextLaunchAt;
    private string? _expect; // "exit" | "restart" | "update"
    private bool _adopting;
    private long _adoptDeadline;
    private StreamWriter? _writer;
    private bool _stop;
    private int _exitCode;
    private long? _probationDeadline;
    private long _healthySince;
    private int _heartbeats;
    private readonly long _smokeDeadline;
    private bool _hangPhaseDone;
    private string? _lastKillReason;

    public WatchdogService(CommandLine args, PlayerPaths paths, ConfigLoadResult cfg, RotatingFileLog log, string selfExe)
    {
        _args = args;
        _paths = paths;
        _cfg = cfg;
        _log = log;
        _selfExe = selfExe;
        _root = Path.GetDirectoryName(selfExe)!;
        _slots = new UpdateSlots(_root);
        _policy = new WatchdogPolicy(WatchdogOptions.FromConfig(cfg.Config.Watchdog) with { MonitorProgress = !args.SmokeTest });
        _pipeName = WdProtocol.PipeName(Process.GetCurrentProcess().SessionId);
        _smokeDeadline = Environment.TickCount64 + 120_000;
    }

    public static string ReadyEventName(int pid) => $@"Local\RdPlayer.Watchdog.Ready.{pid}";

    public static string ResolveShellExe(string root)
    {
        var slotted = Path.Combine(root, "app", "current", UpdateSlots.ShellExeName);
        return File.Exists(slotted) ? slotted : Path.Combine(root, UpdateSlots.ShellExeName);
    }

    private static long Now => Environment.TickCount64;

    public int Run()
    {
        _log.Info("watchdog", $"raiz {_root}; layout {(_slots.IsSlotted ? "com slots (app\\current)" : "portátil")}; pipe {_pipeName}; modo {(_args.SmokeTest ? "smoke-test" : "produção")}");
        _ = Task.Run(() => PipeServerLoopAsync(_cts.Token));

        if (!_args.SmokeTest)
        {
            RecoverUpdateState();
            if (Mutex.TryOpenExisting(AppInfo.ShellMutexName, out var m))
            {
                m.Dispose();
                StartAdoption("casca já em execução");
            }
            else
            {
                _nextLaunchAt = Now;
            }
        }
        else
        {
            _nextLaunchAt = Now;
        }

        try
        {
            MainLoopAsync().GetAwaiter().GetResult();
        }
        finally
        {
            _cts.Cancel();
        }
        _log.Info("watchdog", "encerrando com código " + _exitCode);
        return _exitCode;
    }

    // ============================ laço principal ============================

    private async Task MainLoopAsync()
    {
        while (!_stop)
        {
            using (var tickCts = new CancellationTokenSource(1_000))
            {
                try
                {
                    if (await _events.Reader.WaitToReadAsync(tickCts.Token))
                        while (_events.Reader.TryRead(out var ev)) Handle(ev);
                }
                catch (OperationCanceledException)
                {
                }
            }
            if (_stop) break;
            Tick();
        }
    }

    private void Tick()
    {
        long now = Now;
        if (_args.SmokeTest && now > _smokeDeadline)
        {
            _log.Error("smoke", "tempo esgotado no teste de fumaça supervisionado");
            KillShell("SMOKE_TIMEOUT");
            Stop(ShellExitCodes.SmokeTimeout);
            return;
        }

        if (_shell is null && !_adopting && _nextLaunchAt is long at && now >= at)
        {
            _nextLaunchAt = null;
            LaunchShell();
            return;
        }

        if (_adopting && now > _adoptDeadline)
        {
            _log.Warn("watchdog", "casca existente não respondeu; encerrando processos órfãos e relançando");
            _adopting = false;
            KillOrphanShells();
            _nextLaunchAt = Now + 1_000;
            return;
        }

        if (_shell is not null)
        {
            var action = _policy.Evaluate(now);
            switch (action.Kind)
            {
                case WatchdogActionKind.Reload:
                    _log.Warn("watchdog", "bundle sem progresso → recarregar WebView (" + action.Reason + ")");
                    if (Send(WdProtocol.Simple("reload", action.Reason))) _policy.OnReloadIssued(now);
                    else _policy.OnReloadIssued(now); // conta mesmo assim; sem canal, o reinício virá pela política de heartbeat
                    break;
                case WatchdogActionKind.Restart:
                    _log.Error("watchdog", "casca travada → reiniciar processo (" + action.Reason + ")");
                    KillShell(action.Reason);
                    break;
            }
            if (!_args.SmokeTest) CheckProbation(now);
        }
    }

    private void Handle(Ev ev)
    {
        switch (ev)
        {
            case ClientConnected c:
                _log.Info("pipe", $"casca conectada (pid {c.Pid})");
                break;
            case ClientDisconnected:
                _log.Info("pipe", "casca desconectou do pipe");
                break;
            case ClientMessage cm:
                OnMessage(cm.Msg);
                break;
            case ShellExited se:
                OnShellExited(se);
                break;
        }
    }

    private void OnMessage(WdMessage msg)
    {
        long now = Now;
        switch (msg.Type)
        {
            case "hello":
                {
                    var pid = (int)(msg.GetLong("pid") ?? 0);
                    _log.Info("pipe", $"hello da casca pid {pid} versão {msg.GetString("version")} supervisionada={msg.GetBool("supervised")}");
                    if (_adopting || _shell is null)
                    {
                        try
                        {
                            var p = Process.GetProcessById(pid);
                            AttachShell(p, adopted: true);
                            _adopting = false;
                        }
                        catch (ArgumentException)
                        {
                            _log.Warn("watchdog", "pid informado pela casca não existe");
                        }
                    }
                    if (_args.SmokeTest) Send(WdProtocol.Simple("smoke_ack"));
                    break;
                }
            case "hb":
                _heartbeats++;
                _policy.OnHeartbeat(now, new HeartbeatSample(
                    msg.GetLong("ui_age_ms") ?? 0,
                    msg.GetLong("progress_age_ms"),
                    msg.GetString("state"),
                    msg.GetBool("ready")));
                if (_heartbeats == 1 || _heartbeats % 120 == 0)
                    _log.Info("watchdog", $"heartbeat #{_heartbeats}: ui_age={msg.GetLong("ui_age_ms")} ms, progress_age={msg.GetLong("progress_age_ms")?.ToString() ?? "-"} ms, estado={msg.GetString("state") ?? "-"}");
                if (now - _shellStartedMs > 600_000) _backoff.OnStable();
                break;
            case "bundle_ready":
                _policy.OnHeartbeat(now, new HeartbeatSample(0, 0, "IDLE", true));
                break;
            case "exit_requested":
                _log.Info("watchdog", "saída autorizada pelo técnico (" + (msg.GetString("reason") ?? "") + "); o watchdog também encerra");
                _expect = "exit";
                break;
            case "restart_requested":
                _log.Info("watchdog", "reinício solicitado pela casca: " + (msg.GetString("reason") ?? ""));
                _expect = "restart";
                break;
            case "apply_update":
                _log.Info("update", $"casca pediu para aplicar a atualização {msg.GetString("version")}");
                _expect = "update";
                break;
            default:
                _log.Debug("pipe", "mensagem ignorada: " + msg.Type);
                break;
        }
    }

    // ============================ processo da casca ============================

    private void LaunchShell()
    {
        var exe = ResolveShellExe(_root);
        if (!File.Exists(exe))
        {
            _log.Error("watchdog", "RdPlayer.Shell.exe não encontrado em " + exe + "; nova tentativa em 60 s");
            if (_args.SmokeTest) { Stop(ShellExitCodes.SmokeFailed); return; }
            _nextLaunchAt = Now + 60_000;
            return;
        }
        var psi = new ProcessStartInfo(exe) { UseShellExecute = false, WorkingDirectory = Path.GetDirectoryName(exe)! };
        foreach (var a in _args.ToShellArgs(supervised: true)) psi.ArgumentList.Add(a);
        if (_args.SmokeTest)
        {
            psi.ArgumentList.Add("--smoke-test");
            if (_args.SmokeSimulateHang && !_hangPhaseDone) psi.ArgumentList.Add("--smoke-simulate-hang");
            if (_args.SmokeLog is not null) { psi.ArgumentList.Add("--smoke-log"); psi.ArgumentList.Add(_args.SmokeLog); }
            psi.ArgumentList.Add("--smoke-timeout");
            psi.ArgumentList.Add(_args.SmokeTimeoutSeconds.ToString());
        }
        try
        {
            var p = Process.Start(psi)!;
            _log.Info("watchdog", $"casca iniciada: pid {p.Id} ({exe})");
            AttachShell(p, adopted: false);
        }
        catch (Exception ex)
        {
            _log.Error("watchdog", "falha ao iniciar a casca", ex);
            _nextLaunchAt = Now + _backoff.OnUnexpectedExit(Now, 0);
        }
    }

    private void AttachShell(Process p, bool adopted)
    {
        _shell = p;
        _shellPid = p.Id;
        _shellStartedMs = Now;
        _expect = null;
        _policy.Reset(Now);
        if (adopted) _log.Info("watchdog", "supervisionando casca existente pid " + p.Id);
        try
        {
            p.EnableRaisingEvents = true;
            int pid = p.Id;
            p.Exited += (_, _) =>
            {
                int? code = null;
                try { code = p.ExitCode; } catch (InvalidOperationException) { }
                _events.Writer.TryWrite(new ShellExited(pid, code));
            };
            if (p.HasExited) _events.Writer.TryWrite(new ShellExited(pid, SafeExitCode(p)));
        }
        catch (Exception ex)
        {
            _log.Warn("watchdog", "não foi possível monitorar a saída do pid " + p.Id + ": " + ex.Message);
        }
    }

    private static int? SafeExitCode(Process p)
    {
        try { return p.ExitCode; } catch { return null; }
    }

    private void StartAdoption(string why)
    {
        _adopting = true;
        _adoptDeadline = Now + _cfg.Config.Watchdog.StartupTimeoutMs;
        _log.Info("watchdog", "aguardando a casca em execução se conectar (" + why + ")");
    }

    private void KillShell(string reason)
    {
        var p = _shell;
        if (p is null) return;
        _lastKillReason = reason;
        try
        {
            if (!p.HasExited)
            {
                _log.Warn("watchdog", $"encerrando casca pid {p.Id} à força: {reason}");
                p.Kill(entireProcessTree: true);
            }
        }
        catch (Exception ex)
        {
            _log.Warn("watchdog", "falha ao encerrar a casca: " + ex.Message);
        }
    }

    private void KillOrphanShells()
    {
        int session = Process.GetCurrentProcess().SessionId;
        foreach (var p in Process.GetProcessesByName("RdPlayer.Shell"))
        {
            try
            {
                if (p.SessionId == session)
                {
                    _log.Warn("watchdog", "encerrando casca órfã pid " + p.Id);
                    p.Kill(entireProcessTree: true);
                    p.WaitForExit(5_000);
                }
            }
            catch (Exception)
            {
            }
            finally
            {
                p.Dispose();
            }
        }
    }

    private void OnShellExited(ShellExited se)
    {
        if (se.Pid != _shellPid) return;
        long now = Now;
        long ran = now - _shellStartedMs;
        _shell?.Dispose();
        _shell = null;
        _shellPid = 0;
        lock (_writeLock) _writer = null;
        _policy.Stop();
        string expect = _expect ?? "";
        _expect = null;
        _log.Info("watchdog", $"casca saiu: código {(se.Code?.ToString() ?? "?")}, rodou {ran / 1000} s, esperado={(expect == "" ? "não" : expect)}");

        if (_args.SmokeTest)
        {
            if (_args.SmokeSimulateHang && !_hangPhaseDone)
            {
                _hangPhaseDone = true;
                if (_lastKillReason is "UI_THREAD_HUNG" or "HEARTBEAT_TIMEOUT")
                {
                    long hangDelay = _backoff.OnUnexpectedExit(now, ran);
                    _log.Info("smoke", $"travamento simulado DETECTADO ({_lastKillReason}) após {ran / 1000} s; casca morta; relançando em {hangDelay} ms");
                    _heartbeats = 0;
                    _nextLaunchAt = now + hangDelay;
                    return;
                }
                _log.Error("smoke", "a casca saiu sem o watchdog detectar o travamento simulado (motivo: " + (_lastKillReason ?? "nenhum") + ")");
                Stop(ShellExitCodes.SmokeFailed);
                return;
            }
            _log.Info("smoke", $"teste de fumaça supervisionado: heartbeats recebidos={_heartbeats}; código da casca={se.Code}");
            Stop(se.Code == 0 && _heartbeats > 0 ? 0 : (se.Code is int c && c != 0 ? c : ShellExitCodes.SmokeFailed));
            return;
        }

        if (expect == "exit" || se.Code == ShellExitCodes.ExitRequested)
        {
            Stop(0);
            return;
        }
        if (expect == "update" || se.Code == ShellExitCodes.ApplyUpdate)
        {
            ApplyPendingUpdate();
            _nextLaunchAt = Now;
            return;
        }
        if (expect == "restart" || se.Code == ShellExitCodes.RestartRequested)
        {
            _nextLaunchAt = Now + 500;
            return;
        }
        if (se.Code == ShellExitCodes.AlreadyRunning)
        {
            StartAdoption("outra instância da casca está rodando");
            return;
        }

        // saída inesperada (crash, travamento morto pelo watchdog, Gerenciador de Tarefas)
        if (TryHandleProbationCrash()) return;
        long delay = _backoff.OnUnexpectedExit(now, ran);
        if (_backoff.InCrashLoop) _log.Error("watchdog", $"casca em laço de falhas; próxima tentativa em {delay / 1000} s");
        else _log.Warn("watchdog", $"relançando a casca em {delay / 1000.0:F0} s");
        _nextLaunchAt = now + delay;
    }

    private void Stop(int code)
    {
        _exitCode = code;
        _stop = true;
    }

    // ============================ pipe ============================

    private bool Send(string line)
    {
        lock (_writeLock)
        {
            if (_writer is null) return false;
            try
            {
                _writer.WriteLine(line);
                _writer.Flush();
                return true;
            }
            catch (Exception)
            {
                return false;
            }
        }
    }

    private async Task PipeServerLoopAsync(CancellationToken ct)
    {
        while (!ct.IsCancellationRequested)
        {
            NamedPipeServerStream? server = null;
            try
            {
                server = new NamedPipeServerStream(_pipeName, PipeDirection.InOut, 1, PipeTransmissionMode.Byte,
                    PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                await server.WaitForConnectionAsync(ct).ConfigureAwait(false);

                int clientPid = 0;
                if (GetNamedPipeClientProcessId(server.SafePipeHandle, out var cpid)) clientPid = (int)cpid;
                if (!IsShellProcess(clientPid))
                {
                    _log.Warn("pipe", $"cliente do pipe recusado (pid {clientPid} não é RdPlayer.Shell)");
                    server.Disconnect();
                    continue;
                }

                var writer = new StreamWriter(server, new UTF8Encoding(false)) { AutoFlush = false, NewLine = "\n" };
                var reader = new StreamReader(server, new UTF8Encoding(false));
                lock (_writeLock) _writer = writer;
                _events.Writer.TryWrite(new ClientConnected(clientPid));
                while (!ct.IsCancellationRequested)
                {
                    var line = await reader.ReadLineAsync(ct).ConfigureAwait(false);
                    if (line is null) break;
                    if (WdProtocol.TryParse(line, out var msg)) _events.Writer.TryWrite(new ClientMessage(msg!));
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                break;
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is OperationCanceledException)
            {
                _log.Debug("pipe", "pipe reiniciado: " + ex.GetType().Name);
                try { await Task.Delay(1_000, ct).ConfigureAwait(false); } catch (OperationCanceledException) { break; }
            }
            finally
            {
                lock (_writeLock) _writer = null;
                if (server is not null)
                {
                    _events.Writer.TryWrite(new ClientDisconnected());
                    try { server.Dispose(); } catch { }
                }
            }
        }
    }

    private static bool IsShellProcess(int pid)
    {
        if (pid <= 0) return false;
        try
        {
            using var p = Process.GetProcessById(pid);
            return string.Equals(p.ProcessName, "RdPlayer.Shell", StringComparison.OrdinalIgnoreCase);
        }
        catch (Exception)
        {
            return false;
        }
    }

    // ============================ atualização ============================

    private void RecoverUpdateState()
    {
        var st = StateStore.LoadUpdateState(_paths.UpdateStatePath);
        if (st.Pending is not null)
        {
            _log.Info("update", $"atualização pendente encontrada ({st.Pending.Version}); aplicando antes de iniciar a casca");
            ApplyPendingUpdate();
            return;
        }
        if (st.Probation is not null)
        {
            _log.Warn("update", $"probação da versão {st.Probation.Version} interrompida (queda de energia/reinício?); reiniciando a contagem");
            _probationDeadline = Now + _cfg.Config.Update.HealthTimeoutSeconds * 1000L;
            _healthySince = 0;
        }
    }

    private UpdateApplier Applier => new(_slots, _paths.UpdateStatePath);

    private void ApplyPendingUpdate()
    {
        if (Applier.Load().Pending is null)
        {
            _log.Warn("update", "pedido de atualização sem pacote pendente (update_state.json)");
            return;
        }
        KillOrphanShells(); // nada da casca pode estar usando app\current durante a troca
        var r = Applier.ApplyPending();
        if (r.Ok)
        {
            _probationDeadline = Now + _cfg.Config.Update.HealthTimeoutSeconds * 1000L;
            _healthySince = 0;
            _log.Info("update", $"versão {r.Version} promovida (anterior {r.PreviousVersion ?? "?"}); probação de {_cfg.Config.Update.HealthTimeoutSeconds} s");
        }
        else
        {
            _log.Error("update", $"falha ao aplicar a atualização {r.Version}: {r.Detail} (versão mantida; nova versão na lista negra)");
        }
    }

    private void CheckProbation(long now)
    {
        if (_probationDeadline is not long deadline) return;
        bool healthy = _cfg.Config.Update.RequireBundleHealth
            ? _policy.BundleReady || (_policy.LastProgressAt is long lp && lp > _shellStartedMs)
            : _policy.LastHeartbeatAt is not null;
        if (healthy)
        {
            if (_healthySince == 0) _healthySince = now;
            if (now - _healthySince >= 15_000) CommitProbation();
            return;
        }
        if (now > deadline) RollbackProbation("a nova versão não ficou saudável em " + _cfg.Config.Update.HealthTimeoutSeconds + " s");
    }

    private bool TryHandleProbationCrash()
    {
        if (_probationDeadline is null) return false;
        int crashes = Applier.RegisterProbationCrash();
        if (crashes == 0) { _probationDeadline = null; return false; }
        if (crashes >= 2)
        {
            RollbackProbation($"a nova versão caiu {crashes} vezes na probação");
            return true;
        }
        _nextLaunchAt = Now + 2_000;
        return true;
    }

    private void CommitProbation()
    {
        _probationDeadline = null;
        var version = Applier.Commit();
        _log.Info("update", $"versão {version} aprovada na probação (anterior mantida em app/previous para rollback manual)");
        TrySelfUpdate();
    }

    private void RollbackProbation(string why)
    {
        _probationDeadline = null;
        _log.Error("update", "ROLLBACK: " + why);
        KillShell("ROLLBACK");
        try { _shell?.WaitForExit(10_000); } catch { }
        KillOrphanShells();
        var (ok, version) = Applier.Rollback(why);
        _log.Warn("update", ok ? $"versão {version} revertida; anterior restaurada (e {version} na lista negra)" : $"sem versão anterior para restaurar; mantendo {version}");
        _shell = null;
        _shellPid = 0;
        _nextLaunchAt = Now + 1_000;
    }

    /// <summary>
    /// Se o pacote aprovado trouxe um RdPlayer.Watchdog.exe diferente, troca o próprio executável (renomeia o atual
    /// para .old, copia o novo, inicia com --takeover e só sai quando o novo sinalizar que subiu; senão desfaz).
    /// </summary>
    private void TrySelfUpdate()
    {
        try
        {
            var candidate = Path.Combine(_slots.CurrentDir, UpdateSlots.WatchdogExeName);
            if (!File.Exists(candidate) || string.Equals(Path.GetFullPath(candidate), Path.GetFullPath(_selfExe), StringComparison.OrdinalIgnoreCase)) return;
            if (Sha256Of(candidate) == Sha256Of(_selfExe)) return;
            var old = Path.Combine(_root, "RdPlayer.Watchdog.old.exe");
            if (File.Exists(old)) File.Delete(old);
            File.Move(_selfExe, old);
            File.Copy(candidate, _selfExe);
            var psi = new ProcessStartInfo(_selfExe) { UseShellExecute = false, WorkingDirectory = _root };
            psi.ArgumentList.Add("--takeover");
            psi.ArgumentList.Add(Environment.ProcessId.ToString());
            foreach (var a in _args.ToShellArgs(supervised: false)) psi.ArgumentList.Add(a);
            using var np = Process.Start(psi)!;
            using var ready = new EventWaitHandle(false, EventResetMode.ManualReset, ReadyEventName(np.Id));
            if (ready.WaitOne(20_000))
            {
                _log.Info("update", $"watchdog atualizado; nova instância pid {np.Id} assume");
                Stop(0);
                return;
            }
            _log.Error("update", "novo watchdog não sinalizou; desfazendo a troca");
            try { np.Kill(); } catch { }
            File.Delete(_selfExe);
            File.Move(old, _selfExe);
        }
        catch (Exception ex)
        {
            _log.Error("update", "auto-atualização do watchdog falhou (mantido o atual)", ex);
            if (!File.Exists(_selfExe))
            {
                var old = Path.Combine(_root, "RdPlayer.Watchdog.old.exe");
                try { if (File.Exists(old)) File.Move(old, _selfExe); } catch { }
            }
        }
    }

    private static string Sha256Of(string path)
    {
        using var fs = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(fs));
    }
}
