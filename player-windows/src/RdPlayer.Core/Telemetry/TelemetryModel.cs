using System.Text.Json.Nodes;

namespace RdPlayer.Core.Telemetry;

public sealed record WifiInfo(string Ssid, int? SignalPercent, int? RssiDbm);

/// <summary>Instantâneo de telemetria no formato de <c>telemetry.data</c> (RDPP/1 §4) — sem IP.</summary>
public sealed record TelemetrySnapshot(
    int? CpuPct,
    long? RamUsedMb,
    long? RamTotalMb,
    long? DiskFreeMb,
    long? DiskTotalMb,
    long UptimeS,
    string Os,
    string AppVersion,
    string Resolution,
    string Orientation,
    WifiInfo? Wifi)
{
    public JsonObject ToJson() => new()
    {
        ["cpu_pct"] = CpuPct,
        ["ram_used_mb"] = RamUsedMb,
        ["ram_total_mb"] = RamTotalMb,
        ["disk_free_mb"] = DiskFreeMb,
        ["disk_total_mb"] = DiskTotalMb,
        ["uptime_s"] = UptimeS,
        ["os"] = Os,
        ["app_version"] = AppVersion,
        ["platform"] = "windows",
        ["resolution"] = Resolution,
        ["orientation"] = Orientation,
        ["wifi"] = Wifi is null ? null : new JsonObject { ["ssid"] = Wifi.Ssid, ["rssi_dbm"] = Wifi.RssiDbm },
        ["hdmi_connected"] = null,
    };
}

public static class TelemetryMath
{
    /// <summary>Qualidade de sinal WLAN (0–100) → dBm aproximado (convenção da WLAN API: -100 dBm = 0 %, -50 dBm = 100 %).</summary>
    public static int QualityToDbm(int quality) => Math.Clamp(quality, 0, 100) / 2 - 100;

    /// <summary>CPU % a partir de deltas de GetSystemTimes (kernel inclui idle).</summary>
    public static int? CpuPercent(long idleDelta, long kernelDelta, long userDelta)
    {
        long total = kernelDelta + userDelta;
        if (total <= 0 || idleDelta < 0 || idleDelta > total) return null;
        return (int)Math.Round((total - idleDelta) * 100.0 / total);
    }

    public static string Orientation(int width, int height) => width >= height ? "LANDSCAPE" : "PORTRAIT";

    public static string Resolution(int width, int height) => $"{width}x{height}";

    /// <summary>
    /// Descrição do Windows a partir do registro (HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion).
    /// O registro do Windows 11 ainda diz "Windows 10" em ProductName — corrigido pelo build (≥ 22000).
    /// </summary>
    public static string FormatWindowsVersion(string? productName, string? displayVersion, string? currentBuild, int? ubr)
    {
        var name = string.IsNullOrWhiteSpace(productName) ? "Windows" : productName!.Trim();
        if (int.TryParse(currentBuild, out var build) && build >= 22000 && name.StartsWith("Windows 10", StringComparison.OrdinalIgnoreCase))
            name = "Windows 11" + name.Substring("Windows 10".Length);
        var parts = new List<string> { name };
        if (!string.IsNullOrWhiteSpace(displayVersion)) parts.Add(displayVersion!.Trim());
        if (!string.IsNullOrWhiteSpace(currentBuild)) parts.Add(ubr is int u ? $"(build {currentBuild}.{u})" : $"(build {currentBuild})");
        return string.Join(" ", parts);
    }
}

/// <summary>
/// Parser da saída de <c>netsh wlan show interfaces</c> (fallback da WLAN API nativa).
/// Suporta saídas em inglês, português e espanhol. Não extrai endereço IP/MAC.
/// </summary>
public static class NetshWlanParser
{
    private static readonly string[] StateKeys = { "State", "Estado" };
    private static readonly string[] SignalKeys = { "Signal", "Sinal", "Señal", "Senal" };
    private static readonly string[] ConnectedValues = { "connected", "conectado" };

    public static WifiInfo? Parse(string? output)
    {
        if (string.IsNullOrWhiteSpace(output)) return null;
        var blocks = new List<Dictionary<string, string>>();
        Dictionary<string, string>? cur = null;
        foreach (var rawLine in output.Split('\n'))
        {
            var line = rawLine.TrimEnd('\r');
            int idx = line.IndexOf(" : ", StringComparison.Ordinal);
            if (idx < 0) idx = line.IndexOf(':');
            if (idx <= 0) continue;
            var key = line[..idx].Trim();
            var value = line[(idx + 1)..].TrimStart(':').Trim();
            if (key.Length == 0) continue;
            if (IsNameKey(key))
            {
                cur = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                blocks.Add(cur);
            }
            if (cur is null)
            {
                cur = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                blocks.Add(cur);
            }
            cur.TryAdd(key, value);
        }

        foreach (var b in blocks)
        {
            var state = Get(b, StateKeys);
            if (state is null || !ConnectedValues.Any(v => state.Equals(v, StringComparison.OrdinalIgnoreCase))) continue;
            if (!b.TryGetValue("SSID", out var ssid) || string.IsNullOrEmpty(ssid)) continue;
            int? pct = null;
            var sig = Get(b, SignalKeys);
            if (sig is not null && int.TryParse(sig.TrimEnd('%', ' '), out var p)) pct = Math.Clamp(p, 0, 100);
            int? rssi = null;
            if (b.TryGetValue("Rssi", out var r) && int.TryParse(r, out var rv) && rv is < 0 and > -130) rssi = rv;
            rssi ??= pct is int q ? TelemetryMath.QualityToDbm(q) : null;
            return new WifiInfo(ssid, pct, rssi);
        }
        return null;
    }

    private static bool IsNameKey(string key) =>
        key.Equals("Name", StringComparison.OrdinalIgnoreCase) || key.Equals("Nome", StringComparison.OrdinalIgnoreCase) ||
        key.Equals("Nombre", StringComparison.OrdinalIgnoreCase);

    private static string? Get(Dictionary<string, string> d, string[] keys)
    {
        foreach (var k in keys)
            if (d.TryGetValue(k, out var v)) return v;
        return null;
    }
}
