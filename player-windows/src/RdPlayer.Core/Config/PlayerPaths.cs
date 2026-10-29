namespace RdPlayer.Core.Config;

/// <summary>
/// Caminhos locais. Padrões: config em %PROGRAMDATA%\RdMidiaIndoor\Player\config.json;
/// dados (WebView2/OPFS/IndexedDB, logs, estado, atualizações) em %LOCALAPPDATA%\RdMidiaIndoor\Player.
/// </summary>
public sealed class PlayerPaths
{
    public required string ConfigPath { get; init; }
    public required string DataRoot { get; init; }
    public string WebView2Dir => Path.Combine(DataRoot, "WebView2");
    public string LogsDir => Path.Combine(DataRoot, "logs");
    public string StateDir => Path.Combine(DataRoot, "state");
    public string UpdatesDir => Path.Combine(DataRoot, "updates");
    public string UpdateStatePath => Path.Combine(StateDir, "update_state.json");
    public string ShellStatePath => Path.Combine(StateDir, "shell_state.json");

    public static string DefaultConfigPath =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "RdMidiaIndoor", "Player", "config.json");

    public static string DefaultDataRoot =>
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "RdMidiaIndoor", "Player");

    public static PlayerPaths Resolve(string? configOverride, string? dataRootOverride) => new()
    {
        ConfigPath = Path.GetFullPath(string.IsNullOrWhiteSpace(configOverride) ? DefaultConfigPath : configOverride),
        DataRoot = Path.GetFullPath(string.IsNullOrWhiteSpace(dataRootOverride) ? DefaultDataRoot : dataRootOverride),
    };

    public void EnsureCreated()
    {
        Directory.CreateDirectory(DataRoot);
        Directory.CreateDirectory(LogsDir);
        Directory.CreateDirectory(StateDir);
        Directory.CreateDirectory(UpdatesDir);
    }
}
