namespace RdPlayer.Core;

/// <summary>Argumentos de linha de comando da casca e do watchdog.</summary>
public sealed class CommandLine
{
    public bool SmokeTest { get; private set; }
    public bool Windowed { get; private set; }
    public bool Supervised { get; private set; }
    public bool Autostart { get; private set; }
    public bool ApplyUpdate { get; private set; }
    public bool Adopt { get; private set; }
    public bool Relaunch { get; private set; }
    /// <summary>Somente teste de fumaça: a casca trava a thread de UI de propósito para provar a detecção/reinício pelo watchdog.</summary>
    public bool SmokeSimulateHang { get; private set; }
    public bool InstallAutostart { get; private set; }
    public bool RemoveAutostart { get; private set; }
    public int? TakeoverPid { get; private set; }
    public string? ConfigPath { get; private set; }
    public string? DataRoot { get; private set; }
    public string? BundleDir { get; private set; }
    public string? SmokeLog { get; private set; }
    public int SmokeTimeoutSeconds { get; private set; } = 45;
    public List<string> Unknown { get; } = new();

    public static CommandLine Parse(IReadOnlyList<string> args)
    {
        var c = new CommandLine();
        for (int i = 0; i < args.Count; i++)
        {
            var a = args[i];
            string? Next() => i + 1 < args.Count ? args[++i] : null;
            switch (a.ToLowerInvariant())
            {
                case "--smoke-test": c.SmokeTest = true; break;
                case "--windowed": c.Windowed = true; break;
                case "--supervised": c.Supervised = true; break;
                case "--autostart": c.Autostart = true; break;
                case "--apply-update": c.ApplyUpdate = true; break;
                case "--adopt": c.Adopt = true; break;
                case "--relaunch": c.Relaunch = true; break;
                case "--smoke-simulate-hang": c.SmokeSimulateHang = true; break;
                case "--install-autostart": c.InstallAutostart = true; break;
                case "--remove-autostart": c.RemoveAutostart = true; break;
                case "--takeover":
                    if (int.TryParse(Next(), out var pid) && pid > 0) c.TakeoverPid = pid; else c.Unknown.Add(a);
                    break;
                case "--config": c.ConfigPath = Next(); break;
                case "--data-root": c.DataRoot = Next(); break;
                case "--bundle-dir": c.BundleDir = Next(); break;
                case "--smoke-log": c.SmokeLog = Next(); break;
                case "--smoke-timeout":
                    if (int.TryParse(Next(), out var t) && t is >= 5 and <= 300) c.SmokeTimeoutSeconds = t; else c.Unknown.Add(a);
                    break;
                default: c.Unknown.Add(a); break;
            }
        }
        return c;
    }

    /// <summary>Argumentos a repassar ao relançar a casca (sem flags de uso único).</summary>
    public List<string> ToShellArgs(bool supervised)
    {
        var l = new List<string>();
        if (supervised) l.Add("--supervised");
        if (Windowed) l.Add("--windowed");
        if (ConfigPath is not null) { l.Add("--config"); l.Add(ConfigPath); }
        if (DataRoot is not null) { l.Add("--data-root"); l.Add(DataRoot); }
        if (BundleDir is not null) { l.Add("--bundle-dir"); l.Add(BundleDir); }
        return l;
    }
}
