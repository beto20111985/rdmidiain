using System.Reflection;

namespace RdPlayer.Core;

public static class AppInfo
{
    /// <summary>Versão do pacote Windows (= app_version da plataforma "windows" no RDPP §8).</summary>
    public static string Version { get; } =
        typeof(AppInfo).Assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion?.Split('+')[0]
        ?? "0.0.0";

    public const string Platform = "windows";
    public const string ShellMutexName = @"Local\RdPlayer.Shell";
    public const string ShellSmokeMutexName = @"Local\RdPlayer.Shell.Smoke";
    public const string WatchdogMutexName = @"Local\RdPlayer.Watchdog";
}
