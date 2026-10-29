using System;
using System.IO;
using RdPlayer.Core;
using RdPlayer.Core.Config;
using RdPlayer.Core.Logging;
using RdPlayer.Core.Update;

namespace RdPlayer.Shell;

internal sealed class ShellContext
{
    public const string ShellUiHost = "shell.rd.local";

    public required CommandLine Args { get; init; }
    public required PlayerPaths Paths { get; init; }
    public required ConfigLoadResult ConfigResult { get; init; }
    public required RotatingFileLog Log { get; init; }

    public PlayerConfig Config => ConfigResult.Config;
    public bool Smoke => Args.SmokeTest;
    /// <summary>Modo quiosque: tela cheia, bloqueios, cursor oculto, energia. Desligado no teste de fumaça e em --windowed.</summary>
    public bool Kiosk => !Args.SmokeTest && !Args.Windowed;
    public int ExitCode { get; set; } = ShellExitCodes.Crash;

    public string ExeDir => AppContext.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
    public string ExePath => Environment.ProcessPath ?? Path.Combine(ExeDir, UpdateSlots.ShellExeName);

    public string BundleDir => Path.GetFullPath(Args.BundleDir ?? Config.Web.BundleDir ?? Path.Combine(ExeDir, "wwwroot"));
    public string ShellUiDir => Path.Combine(ExeDir, "shell-ui");
    public string PlayerOrigin => "https://" + Config.Web.VirtualHost;
    public string ShellUiOrigin => "https://" + ShellUiHost;

    /// <summary>
    /// Raiz da instalação com slots (&lt;raiz&gt;\app\current\RdPlayer.Shell.exe) ou null em layout portátil.
    /// </summary>
    public string? InstallRoot
    {
        get
        {
            var dir = new DirectoryInfo(ExeDir);
            if (dir.Name.Equals("current", StringComparison.OrdinalIgnoreCase) &&
                dir.Parent is { } app && app.Name.Equals("app", StringComparison.OrdinalIgnoreCase) && app.Parent is { } root)
                return root.FullName;
            return null;
        }
    }

    public string WatchdogExePath => Path.Combine(InstallRoot ?? ExeDir, UpdateSlots.WatchdogExeName);
}
