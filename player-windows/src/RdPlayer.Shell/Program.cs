using System;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using RdPlayer.Core;
using RdPlayer.Core.Config;
using RdPlayer.Core.Logging;

namespace RdPlayer.Shell;

internal static class Program
{
    [STAThread]
    private static int Main(string[] argv)
    {
        System.Text.Encoding.RegisterProvider(System.Text.CodePagesEncodingProvider.Instance); // saída OEM do netsh
        var args = CommandLine.Parse(argv);
        var paths = PlayerPaths.Resolve(args.ConfigPath, args.DataRoot);
        try
        {
            paths.EnsureCreated();
        }
        catch (Exception ex)
        {
            MessageBox.Show("RD Mídia Indoor Player: não foi possível criar a pasta de dados " + paths.DataRoot + "\n" + ex.Message,
                "RD Mídia Indoor Player", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return ShellExitCodes.Crash;
        }

        var cfg = ConfigLoader.Load(paths.ConfigPath);
        var log = new RotatingFileLog(paths.LogsDir, args.SmokeTest ? "shell-smoke" : "shell",
            RotatingFileLog.ParseLevel(cfg.Config.Logging.Level),
            Math.Max(1, cfg.Config.Logging.MaxFileMb) * 1024L * 1024L, cfg.Config.Logging.MaxFiles);

        log.Info("main", $"RdPlayer.Shell {AppInfo.Version} iniciando; pid={Environment.ProcessId}; sessão={System.Diagnostics.Process.GetCurrentProcess().SessionId}; " +
                         $"smoke={args.SmokeTest}; windowed={args.Windowed}; supervised={args.Supervised}; config={paths.ConfigPath}; dados={paths.DataRoot}");
        foreach (var u in args.Unknown) log.Warn("main", "argumento desconhecido ignorado: " + u);
        foreach (var e in cfg.Errors) log.Error("config", e);
        foreach (var w in cfg.Warnings) log.Warn("config", w);

        // instância única (o teste de fumaça usa um mutex separado e não interfere num player em execução)
        var mutexName = args.SmokeTest ? AppInfo.ShellSmokeMutexName : AppInfo.ShellMutexName;
        using var mutex = new Mutex(false, mutexName);
        bool owned;
        try
        {
            owned = mutex.WaitOne(args.Supervised || args.Relaunch ? 15_000 : 0);
        }
        catch (AbandonedMutexException)
        {
            owned = true; // instância anterior morreu sem liberar
            log.Warn("main", "mutex abandonado por instância anterior (encerrada abruptamente)");
        }
        if (!owned)
        {
            log.Warn("main", "outra instância da casca já está em execução; saindo");
            log.Dispose();
            return ShellExitCodes.AlreadyRunning;
        }

        var ctx = new ShellContext { Args = args, Paths = paths, ConfigResult = cfg, Log = log };
        try
        {
            Application.SetHighDpiMode(HighDpiMode.PerMonitorV2);
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.CatchException);
            Application.ThreadException += (_, e) => log.Error("ui", "exceção não tratada na thread de UI", e.Exception);
            AppDomain.CurrentDomain.UnhandledException += (_, e) => log.Error("main", "exceção fatal", e.ExceptionObject as Exception);
            TaskScheduler.UnobservedTaskException += (_, e) =>
            {
                log.Error("main", "exceção de Task não observada", e.Exception);
                e.SetObserved();
            };

            using (var form = new MainForm(ctx))
            {
                Application.Run(form);
            }
            log.Info("main", $"encerrando; código de saída {ctx.ExitCode}");
            return ctx.ExitCode;
        }
        catch (Exception ex)
        {
            log.Error("main", "falha fatal na casca", ex);
            return ShellExitCodes.Crash;
        }
        finally
        {
            try { mutex.ReleaseMutex(); } catch (ApplicationException) { /* não era dono */ }
            log.Dispose();
        }
    }
}
