using System;
using System.Diagnostics;
using System.IO;
using System.Threading;
using RdPlayer.Core;
using RdPlayer.Core.Config;
using RdPlayer.Core.Logging;
using RdPlayer.Core.Platform;

namespace RdPlayer.Watchdog;

internal static class Program
{
    private static int Main(string[] argv)
    {
        var args = CommandLine.Parse(argv);
        var paths = PlayerPaths.Resolve(args.ConfigPath, args.DataRoot);
        try
        {
            paths.EnsureCreated();
        }
        catch (Exception)
        {
            return 1;
        }
        var cfg = ConfigLoader.Load(paths.ConfigPath);
        using var log = new RotatingFileLog(paths.LogsDir, args.SmokeTest ? "watchdog-smoke" : "watchdog",
            RotatingFileLog.ParseLevel(cfg.Config.Logging.Level),
            Math.Max(1, cfg.Config.Logging.MaxFileMb) * 1024L * 1024L, cfg.Config.Logging.MaxFiles);
        var self = Environment.ProcessPath ?? Path.Combine(AppContext.BaseDirectory, "RdPlayer.Watchdog.exe");
        log.Info("main", $"RdPlayer.Watchdog {AppInfo.Version} iniciando; pid={Environment.ProcessId}; args=[{string.Join(' ', argv)}]");
        foreach (var e in cfg.Errors) log.Warn("config", e);

        try
        {
            if (args.InstallAutostart)
            {
                var (ok, detail) = Autostart.Enable(cfg.Config.Autostart.Method, self, Path.Combine(paths.DataRoot, "tmp"));
                log.Info("autostart", $"ligar ({cfg.Config.Autostart.Method}): {(ok ? "ok" : "falhou")} — {detail}");
                return ok ? 0 : 1;
            }
            if (args.RemoveAutostart)
            {
                var (ok, detail) = Autostart.Disable();
                log.Info("autostart", $"desligar: {(ok ? "ok" : "falhou")} — {detail}");
                return ok ? 0 : 1;
            }

            if (!cfg.Config.Watchdog.Enabled && !args.SmokeTest)
            {
                log.Warn("main", "watchdog.enabled=false: iniciando a casca sem supervisão e saindo");
                var shell = WatchdogService.ResolveShellExe(AppContext.BaseDirectory);
                if (File.Exists(shell)) Process.Start(new ProcessStartInfo(shell) { UseShellExecute = false, WorkingDirectory = Path.GetDirectoryName(shell)! })?.Dispose();
                return 0;
            }

            // auto-atualização do watchdog: a nova instância sinaliza que subiu antes de assumir o mutex
            if (args.TakeoverPid is int oldPid)
            {
                // o handle fica aberto até o antigo sair, para que ele veja o evento sinalizado
                using var ready = new EventWaitHandle(false, EventResetMode.ManualReset, WatchdogService.ReadyEventName(Environment.ProcessId));
                ready.Set();
                try
                {
                    using var old = Process.GetProcessById(oldPid);
                    if (!old.WaitForExit(30_000)) log.Warn("main", "watchdog antigo não saiu em 30 s");
                }
                catch (ArgumentException)
                {
                    // já saiu
                }
            }

            var mutexName = args.SmokeTest ? AppInfo.WatchdogMutexName + ".Smoke" : AppInfo.WatchdogMutexName;
            using var mutex = new Mutex(false, mutexName);
            bool owned;
            try
            {
                owned = mutex.WaitOne(args.TakeoverPid is not null || args.ApplyUpdate ? 30_000 : 0);
            }
            catch (AbandonedMutexException)
            {
                owned = true;
            }
            if (!owned)
            {
                log.Info("main", "outro watchdog já está em execução; saindo");
                return 0;
            }
            if (args.SmokeTest && Mutex.TryOpenExisting(AppInfo.WatchdogMutexName, out var prod))
            {
                prod.Dispose();
                log.Error("main", "teste de fumaça recusado: há um watchdog de produção em execução nesta sessão");
                return 12;
            }
            try
            {
                var service = new WatchdogService(args, paths, cfg, log, self);
                return service.Run();
            }
            finally
            {
                try { mutex.ReleaseMutex(); } catch (ApplicationException) { }
            }
        }
        catch (Exception ex)
        {
            log.Error("main", "falha fatal no watchdog", ex);
            return 1;
        }
    }
}
