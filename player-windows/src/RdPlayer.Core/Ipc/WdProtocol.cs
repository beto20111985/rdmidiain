using System.Text.Json;
using System.Text.Json.Nodes;

namespace RdPlayer.Core.Ipc;

/// <summary>
/// Protocolo casca ↔ watchdog: JSON por linha num named pipe local (somente usuário atual).
/// Casca → watchdog: hello, hb, exit_requested, restart_requested, apply_update, bundle_ready.
/// Watchdog → casca: reload, shutdown, update_result.
/// </summary>
public static class WdProtocol
{
    public const int MaxLineChars = 16 * 1024;

    public static string PipeName(int sessionId) => $"RdPlayer.Watchdog.S{sessionId}";

    public static string Hello(int pid, string version, bool supervised) => Line(new JsonObject
    {
        ["t"] = "hello",
        ["pid"] = pid,
        ["version"] = version,
        ["supervised"] = supervised,
    });

    public static string Heartbeat(int pid, long uiAgeMs, long? progressAgeMs, string? state, bool ready) => Line(new JsonObject
    {
        ["t"] = "hb",
        ["pid"] = pid,
        ["ui_age_ms"] = uiAgeMs,
        ["progress_age_ms"] = progressAgeMs,
        ["state"] = state,
        ["ready"] = ready,
    });

    public static string Simple(string type, string? reason = null)
    {
        var o = new JsonObject { ["t"] = type };
        if (reason is not null) o["reason"] = reason;
        return Line(o);
    }

    public static string ApplyUpdate(string version, string stagedDir) => Line(new JsonObject
    {
        ["t"] = "apply_update",
        ["version"] = version,
        ["staged_dir"] = stagedDir,
    });

    private static string Line(JsonObject o) => o.ToJsonString();

    public static bool TryParse(string? line, out WdMessage? msg)
    {
        msg = null;
        if (string.IsNullOrWhiteSpace(line) || line.Length > MaxLineChars) return false;
        try
        {
            if (JsonNode.Parse(line) is not JsonObject o) return false;
            if (o["t"] is not JsonValue tv || !tv.TryGetValue<string>(out var t) || string.IsNullOrEmpty(t) || t.Length > 32) return false;
            msg = new WdMessage(t, o);
            return true;
        }
        catch (JsonException)
        {
            return false;
        }
    }
}

public sealed record WdMessage(string Type, JsonObject Body)
{
    public string? GetString(string name) =>
        Body[name] is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;

    public long? GetLong(string name)
    {
        if (Body[name] is not JsonValue v) return null;
        if (v.TryGetValue<long>(out var l)) return l;
        if (v.TryGetValue<int>(out var i)) return i;
        if (v.TryGetValue<JsonElement>(out var e) && e.ValueKind == JsonValueKind.Number && e.TryGetInt64(out var el)) return el;
        return null;
    }

    public bool GetBool(string name) =>
        Body[name] is JsonValue v && ((v.TryGetValue<bool>(out var b) && b) ||
                                      (v.TryGetValue<JsonElement>(out var e) && e.ValueKind == JsonValueKind.True));
}
