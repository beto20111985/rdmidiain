using System.Text.Json;
using System.Text.RegularExpressions;

namespace RdPlayer.Core.Update;

/// <summary>Resposta de <c>GET /api/v1/player/app-update</c> (RDPP/1 §8).</summary>
public sealed partial record AppUpdateInfo(string Version, string Url, string Sha256, long Size, string Signature, string? KeyId, bool Mandatory)
{
    public const long MaxSize = 2L * 1024 * 1024 * 1024;

    [GeneratedRegex("^[0-9a-f]{64}$")]
    private static partial Regex Sha256Hex();

    public static bool TryParse(string json, out AppUpdateInfo? info, out string? error)
    {
        info = null;
        try
        {
            using var doc = JsonDocument.Parse(json);
            return TryParse(doc.RootElement, out info, out error);
        }
        catch (JsonException ex)
        {
            error = "JSON inválido: " + ex.Message;
            return false;
        }
    }

    public static bool TryParse(JsonElement e, out AppUpdateInfo? info, out string? error)
    {
        info = null;
        error = null;
        if (e.ValueKind != JsonValueKind.Object) { error = "objeto esperado"; return false; }

        if (!TryGetString(e, "version", out var version) || !AppVersion.TryParse(version, out _)) { error = "version inválida"; return false; }
        if (!TryGetString(e, "url", out var url) || string.IsNullOrWhiteSpace(url) || url.Length > 2048) { error = "url inválida"; return false; }
        if (!TryGetString(e, "sha256", out var sha) || !Sha256Hex().IsMatch(sha!)) { error = "sha256 deve ter 64 hex minúsculos"; return false; }
        if (!e.TryGetProperty("size", out var sizeEl) || sizeEl.ValueKind != JsonValueKind.Number || !sizeEl.TryGetInt64(out var size) || size <= 0 || size > MaxSize)
        { error = "size inválido"; return false; }
        if (!TryGetString(e, "signature", out var sig) || string.IsNullOrEmpty(sig)) { error = "signature ausente"; return false; }
        string? keyId = null;
        if (e.TryGetProperty("key_id", out var kid) && kid.ValueKind == JsonValueKind.String) keyId = kid.GetString();
        bool mandatory = e.TryGetProperty("mandatory", out var man) && man.ValueKind == JsonValueKind.True;

        info = new AppUpdateInfo(version!, url!, sha!, size, sig!, keyId, mandatory);
        return true;
    }

    private static bool TryGetString(JsonElement e, string name, out string? value)
    {
        value = null;
        if (!e.TryGetProperty(name, out var p) || p.ValueKind != JsonValueKind.String) return false;
        value = p.GetString();
        return value is not null;
    }
}
