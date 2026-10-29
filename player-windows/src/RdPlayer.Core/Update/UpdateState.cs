using System.Text.Json;
using System.Text.Json.Serialization.Metadata;
using RdPlayer.Core.Config;
using RdPlayer.Core.IO;

namespace RdPlayer.Core.Update;

public sealed class PendingUpdate
{
    public string Version { get; set; } = "";
    public string StagedDir { get; set; } = "";
    public string Sha256 { get; set; } = "";
    public long CreatedMs { get; set; }
}

public sealed class ProbationInfo
{
    public string Version { get; set; } = "";
    public string? PreviousVersion { get; set; }
    public long StartedMs { get; set; }
    public int Crashes { get; set; }
}

public sealed class UpdateResultInfo
{
    public string Version { get; set; } = "";
    /// <summary>COMMITTED | ROLLED_BACK | FAILED</summary>
    public string Result { get; set; } = "";
    public string? Detail { get; set; }
    public long AtMs { get; set; }
}

/// <summary>Estado persistido do mecanismo de atualização (arquivo update_state.json).</summary>
public sealed class UpdateState
{
    public PendingUpdate? Pending { get; set; }
    public ProbationInfo? Probation { get; set; }
    public List<string> BadVersions { get; set; } = new();
    public UpdateResultInfo? LastResult { get; set; }
}

/// <summary>Estado persistido da casca (shell_state.json).</summary>
public sealed class ShellState
{
    public long? LastRemoteRebootMs { get; set; }
    public long? LastStartMs { get; set; }
    /// <summary>Último resultado de atualização já informado ao bundle (evento update_status).</summary>
    public long? LastReportedUpdateMs { get; set; }
    /// <summary>PIN técnico do servidor (set_tech_pin repassado pelo bundle), no formato de <see cref="Security.PinHash"/>. Só hash.</summary>
    public string? ServerTechPinHash { get; set; }
    public long? ServerTechPinSetAtMs { get; set; }
}

public static class StateStore
{
    public static T Load<T>(string path, JsonTypeInfo<T> typeInfo, Func<T> factory)
    {
        try
        {
            if (!File.Exists(path)) return factory();
            var json = File.ReadAllText(path);
            return JsonSerializer.Deserialize(json, typeInfo) ?? factory();
        }
        catch (Exception ex) when (ex is JsonException || ex is IOException || ex is UnauthorizedAccessException)
        {
            return factory();
        }
    }

    public static void Save<T>(string path, T value, JsonTypeInfo<T> typeInfo)
    {
        FileUtil.AtomicWriteAllText(path, JsonSerializer.Serialize(value, typeInfo));
    }

    public static UpdateState LoadUpdateState(string path) => Load(path, CoreJsonContext.Default.UpdateState, () => new UpdateState());
    public static void SaveUpdateState(string path, UpdateState s) => Save(path, s, CoreJsonContext.Default.UpdateState);
    public static ShellState LoadShellState(string path) => Load(path, CoreJsonContext.Default.ShellState, () => new ShellState());
    public static void SaveShellState(string path, ShellState s) => Save(path, s, CoreJsonContext.Default.ShellState);
}
