using System.Text.Json;
using System.Text.RegularExpressions;
using RdPlayer.Core.Crypto;
using RdPlayer.Core.Input;
using RdPlayer.Core.Security;

namespace RdPlayer.Core.Config;

public sealed class ConfigLoadResult
{
    public required PlayerConfig Config { get; init; }
    public required string Path { get; init; }
    public bool Exists { get; init; }
    public List<string> Errors { get; } = new();
    public List<string> Warnings { get; } = new();
    public bool Ok => Errors.Count == 0;
}

public static partial class ConfigLoader
{
    public const int MaxConfigBytes = 256 * 1024;

    [GeneratedRegex(@"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")]
    private static partial Regex HostName();

    [GeneratedRegex(@"^\\\\\.\\DISPLAY\d{1,2}$", RegexOptions.IgnoreCase)]
    private static partial Regex DisplayDevice();

    public static ConfigLoadResult Load(string path)
    {
        if (!File.Exists(path))
        {
            var r = new ConfigLoadResult { Config = new PlayerConfig(), Path = path, Exists = false };
            r.Errors.Add("CONFIG_MISSING: arquivo de configuração não encontrado: " + path);
            return r;
        }
        string json;
        try
        {
            var info = new FileInfo(path);
            if (info.Length > MaxConfigBytes)
            {
                var big = new ConfigLoadResult { Config = new PlayerConfig(), Path = path, Exists = true };
                big.Errors.Add("CONFIG_TOO_LARGE");
                return big;
            }
            json = File.ReadAllText(path);
        }
        catch (Exception ex) when (ex is IOException || ex is UnauthorizedAccessException)
        {
            var r = new ConfigLoadResult { Config = new PlayerConfig(), Path = path, Exists = true };
            r.Errors.Add("CONFIG_UNREADABLE: " + ex.GetType().Name);
            return r;
        }
        return Parse(json, path);
    }

    public static ConfigLoadResult Parse(string json, string path = "(memória)")
    {
        PlayerConfig? cfg;
        try
        {
            cfg = JsonSerializer.Deserialize(json, CoreJsonContext.Default.PlayerConfig);
        }
        catch (JsonException ex)
        {
            var r = new ConfigLoadResult { Config = new PlayerConfig(), Path = path, Exists = true };
            r.Errors.Add($"CONFIG_INVALID_JSON: linha {ex.LineNumber}, posição {ex.BytePositionInLine}");
            return r;
        }
        var result = new ConfigLoadResult { Config = cfg ?? new PlayerConfig(), Path = path, Exists = true };
        Validate(result.Config, result.Errors, result.Warnings);
        return result;
    }

    public static void Validate(PlayerConfig c, List<string> errors, List<string> warnings)
    {
        // nulos vindos de JSON ("web": null)
        c.Web ??= new(); c.Display ??= new(); c.Kiosk ??= new(); c.Power ??= new(); c.Watchdog ??= new();
        c.Autostart ??= new(); c.Update ??= new(); c.Telemetry ??= new(); c.Logging ??= new(); c.Dev ??= new();
        c.Update.PublicKeys ??= new();

        if (c.Schema is not null && c.Schema != "rd.player.windows.config/1")
            warnings.Add("schema desconhecido: " + c.Schema);

        bool insecureOk = c.Dev.AllowInsecureApi;
        if (insecureOk) warnings.Add("dev.allow_insecure_api=true: SOMENTE para desenvolvimento local");

        if (string.IsNullOrWhiteSpace(c.ApiBaseUrl)) errors.Add("api_base_url obrigatório");
        else if (!IsValidBaseUrl(c.ApiBaseUrl, insecureOk ? new[] { "https", "http" } : new[] { "https" }))
            errors.Add("api_base_url inválido (use https://host[:porta][/caminho], sem query/credenciais)");

        if (!string.IsNullOrWhiteSpace(c.WsBaseUrl) && !IsValidBaseUrl(c.WsBaseUrl, insecureOk ? new[] { "wss", "ws" } : new[] { "wss" }))
            errors.Add("ws_base_url inválido (use wss://)");

        if (!string.IsNullOrWhiteSpace(c.PanelUrl) && !IsValidBaseUrl(c.PanelUrl, new[] { "https", "http" }))
            errors.Add("panel_url inválido");

        if (c.Web.Source is not ("local" or "remote")) errors.Add("web.source deve ser \"local\" ou \"remote\"");
        if (c.Web.Source == "remote" && (string.IsNullOrWhiteSpace(c.Web.RemoteUrl) || !IsValidBaseUrl(c.Web.RemoteUrl, new[] { "https" }, allowQuery: true)))
            errors.Add("web.remote_url https obrigatório quando web.source=remote");
        if (!HostName().IsMatch(c.Web.VirtualHost ?? "")) errors.Add("web.virtual_host inválido");
        if (string.IsNullOrWhiteSpace(c.Web.Entry) || c.Web.Entry.Contains("..") || c.Web.Entry.StartsWith('/') || c.Web.Entry.Contains(':'))
            errors.Add("web.entry inválido");

        var mon = c.Display.Monitor ?? "primary";
        if (mon != "primary" && !int.TryParse(mon, out _) && !DisplayDevice().IsMatch(mon))
            errors.Add("display.monitor inválido (\"primary\", índice ou \\\\.\\DISPLAYn)");

        if (!KeyChord.TryParse(c.Kiosk.TechShortcut, out var chord) || (!chord.HasModifier && !chord.IsFunctionKey))
            errors.Add("kiosk.tech_shortcut inválido (ex.: Ctrl+Alt+Shift+F12)");
        if (!string.IsNullOrEmpty(c.Kiosk.TechPinHash) && !PinHash.IsValidFormat(c.Kiosk.TechPinHash))
            errors.Add("kiosk.tech_pin_hash em formato inválido");
        if (string.IsNullOrEmpty(c.Kiosk.TechPinHash))
            warnings.Add("kiosk.tech_pin_hash ausente: menu técnico desativado até o servidor enviar set_tech_pin (saída só pelo Gerenciador de Tarefas)");
        if (c.Kiosk.PinMaxAttempts is < 1 or > 20) errors.Add("kiosk.pin_max_attempts fora de 1..20");
        if (c.Kiosk.PinLockoutSeconds is < 10 or > 86400) errors.Add("kiosk.pin_lockout_seconds fora de 10..86400");

        if (c.Power.RebootMinIntervalMinutes is < 5 or > 10080) errors.Add("power.reboot_min_interval_minutes fora de 5..10080");
        if (c.Power.RebootDelaySeconds is < 5 or > 600) errors.Add("power.reboot_delay_seconds fora de 5..600");

        var w = c.Watchdog;
        if (w.HeartbeatIntervalMs is < 1000 or > 30000) errors.Add("watchdog.heartbeat_interval_ms fora de 1000..30000");
        if (w.HangTimeoutMs < w.HeartbeatIntervalMs * 2) errors.Add("watchdog.hang_timeout_ms deve ser >= 2x heartbeat_interval_ms");
        if (w.StartupTimeoutMs is < 10000 or > 600000) errors.Add("watchdog.startup_timeout_ms fora de 10000..600000");
        if (w.ProgressTimeoutMs is < 10000 or > 3600000) errors.Add("watchdog.progress_timeout_ms fora de 10000..3600000");
        if (w.StartupGraceMs is < 0 or > 3600000) errors.Add("watchdog.startup_grace_ms fora de 0..3600000");
        if (w.ReloadGraceMs is < 5000 or > 3600000) errors.Add("watchdog.reload_grace_ms fora de 5000..3600000");
        if (w.MaxReloadsBeforeRestart is < 1 or > 20) errors.Add("watchdog.max_reloads_before_restart fora de 1..20");
        if (w.ReloadWindowMs is < 60000 or > 86400000) errors.Add("watchdog.reload_window_ms fora de 60000..86400000");

        if (c.Autostart.Method is not ("task" or "run")) errors.Add("autostart.method deve ser \"task\" ou \"run\"");

        var u = c.Update;
        if (u.CheckIntervalHours is < 1 or > 168) errors.Add("update.check_interval_hours fora de 1..168");
        if (u.HealthTimeoutSeconds is < 30 or > 1800) errors.Add("update.health_timeout_seconds fora de 30..1800");
        if (u.MaxPackageMb is < 1 or > 2048) errors.Add("update.max_package_mb fora de 1..2048");
        foreach (var k in u.PublicKeys)
        {
            if (k is null || !Base64Url.TryDecode(k.PublicKey, out var spki) || !Es256.IsP256Spki(spki))
                errors.Add($"update.public_keys[{k?.KeyId}] não é SPKI P-256 em base64url");
        }
        if (u.Enabled && u.PublicKeys.Count == 0)
            warnings.Add("update.public_keys vazio: atualizações serão recusadas (UPDATE_KEYS_NOT_CONFIGURED)");

        if (c.Logging.Level is not ("debug" or "info" or "warn" or "error")) errors.Add("logging.level inválido");
        if (c.Logging.MaxFileMb is < 1 or > 100) errors.Add("logging.max_file_mb fora de 1..100");
        if (c.Logging.MaxFiles is < 1 or > 100) errors.Add("logging.max_files fora de 1..100");
    }

    internal static bool IsValidBaseUrl(string? s, string[] schemes, bool allowQuery = false)
    {
        if (!Uri.TryCreate(s, UriKind.Absolute, out var u)) return false;
        if (!schemes.Contains(u.Scheme, StringComparer.OrdinalIgnoreCase)) return false;
        if (!string.IsNullOrEmpty(u.UserInfo)) return false;
        if (!allowQuery && (!string.IsNullOrEmpty(u.Query) || !string.IsNullOrEmpty(u.Fragment))) return false;
        return !string.IsNullOrEmpty(u.Host);
    }

    /// <summary>ws_base_url efetivo: o configurado ou derivado de api_base_url (https→wss, http→ws).</summary>
    public static string? EffectiveWsBaseUrl(PlayerConfig c)
    {
        if (!string.IsNullOrWhiteSpace(c.WsBaseUrl)) return c.WsBaseUrl!.TrimEnd('/');
        if (!Uri.TryCreate(c.ApiBaseUrl, UriKind.Absolute, out var u)) return null;
        var b = new UriBuilder(u) { Scheme = u.Scheme == "https" ? "wss" : "ws", Port = u.IsDefaultPort ? -1 : u.Port };
        return b.Uri.ToString().TrimEnd('/');
    }
}
