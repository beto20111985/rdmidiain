using System;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using RdPlayer.Core;
using RdPlayer.Core.Ipc;
using RdPlayer.Core.Logging;
using RdPlayer.Core.Watchdog;

namespace RdPlayer.Shell;

/// <summary>
/// Cliente do named pipe do watchdog (somente usuário atual). Envia hello + heartbeat a cada ~5 s
/// e recebe comandos (reload, shutdown). Reconecta sozinho se o watchdog reiniciar.
/// </summary>
internal sealed class WatchdogLink : IDisposable
{
    private readonly string _pipeName;
    private readonly int _intervalMs;
    private readonly bool _supervised;
    private readonly RotatingFileLog _log;
    private readonly Func<HeartbeatSample> _sample;
    private readonly Action<WdMessage> _onMessage;
    private readonly CancellationTokenSource _cts = new();
    private readonly object _writeLock = new();
    private StreamWriter? _writer;
    private Task? _loop;
    private long _lastConnectedMs;

    public WatchdogLink(string pipeName, int intervalMs, bool supervised, RotatingFileLog log, Func<HeartbeatSample> sample, Action<WdMessage> onMessage)
    {
        _pipeName = pipeName;
        _intervalMs = intervalMs;
        _supervised = supervised;
        _log = log;
        _sample = sample;
        _onMessage = onMessage;
        _lastConnectedMs = Environment.TickCount64;
    }

    public bool IsConnected => Volatile.Read(ref _writer) is not null;
    /// <summary>Momento (TickCount64) da última conexão ativa — ou do início, se nunca conectou.</summary>
    public long LastConnectedMs => Interlocked.Read(ref _lastConnectedMs);

    public void Start() => _loop = Task.Run(() => RunAsync(_cts.Token));

    public bool Send(string line)
    {
        lock (_writeLock)
        {
            var w = _writer;
            if (w is null) return false;
            try
            {
                w.WriteLine(line);
                w.Flush();
                return true;
            }
            catch (Exception ex) when (ex is IOException || ex is ObjectDisposedException || ex is InvalidOperationException)
            {
                return false;
            }
        }
    }

    private async Task RunAsync(CancellationToken ct)
    {
        bool loggedWaiting = false;
        while (!ct.IsCancellationRequested)
        {
            NamedPipeClientStream? pipe = null;
            try
            {
                pipe = new NamedPipeClientStream(".", _pipeName, PipeDirection.InOut, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
                await pipe.ConnectAsync(2_000, ct).ConfigureAwait(false);
                loggedWaiting = false;
                _log.Info("watchdog-link", "conectado ao watchdog (" + _pipeName + ")");
                var writer = new StreamWriter(pipe, new UTF8Encoding(false)) { AutoFlush = false, NewLine = "\n" };
                var reader = new StreamReader(pipe, new UTF8Encoding(false));
                lock (_writeLock) _writer = writer;
                Interlocked.Exchange(ref _lastConnectedMs, Environment.TickCount64);
                Send(WdProtocol.Hello(Environment.ProcessId, AppInfo.Version, _supervised));

                var readTask = ReadLoopAsync(reader, ct);
                while (!ct.IsCancellationRequested && pipe.IsConnected && !readTask.IsCompleted)
                {
                    var s = _sample();
                    if (!Send(WdProtocol.Heartbeat(Environment.ProcessId, s.UiAgeMs, s.ProgressAgeMs, s.BundleState, s.BundleReady))) break;
                    Interlocked.Exchange(ref _lastConnectedMs, Environment.TickCount64);
                    await Task.WhenAny(readTask, Task.Delay(_intervalMs, ct)).ConfigureAwait(false);
                }
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                break;
            }
            catch (TimeoutException)
            {
                if (!loggedWaiting)
                {
                    _log.Debug("watchdog-link", "watchdog não disponível; tentando novamente");
                    loggedWaiting = true;
                }
            }
            catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException || ex is OperationCanceledException)
            {
                _log.Warn("watchdog-link", "conexão com o watchdog perdida: " + ex.GetType().Name);
            }
            finally
            {
                lock (_writeLock) _writer = null;
                try { pipe?.Dispose(); } catch { /* ignora */ }
            }
            try { await Task.Delay(2_000, ct).ConfigureAwait(false); } catch (OperationCanceledException) { break; }
        }
    }

    private async Task ReadLoopAsync(StreamReader reader, CancellationToken ct)
    {
        try
        {
            while (!ct.IsCancellationRequested)
            {
                var line = await reader.ReadLineAsync(ct).ConfigureAwait(false);
                if (line is null) break;
                if (WdProtocol.TryParse(line, out var msg)) _onMessage(msg!);
            }
        }
        catch (Exception ex) when (ex is IOException || ex is OperationCanceledException || ex is ObjectDisposedException)
        {
        }
    }

    public void Dispose()
    {
        _cts.Cancel();
        try { _loop?.Wait(1_000); } catch { /* ignora */ }
        lock (_writeLock) _writer = null;
        _cts.Dispose();
    }
}
