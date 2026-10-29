using System.Text.Json;
using System.Text.Json.Nodes;
using RdPlayer.Core.Crypto;

namespace RdPlayer.Core.Security;

/// <summary>
/// PIN técnico definido pelo servidor (RDPP §4 <c>set_tech_pin</c>): <c>{algo:"pbkdf2-sha256", iterations, salt_b64u, hash_b64u}</c>.
/// O bundle recebe o comando pelo WS, guarda no IndexedDB e repassa o MESMO registro à casca pela notificação
/// <c>tech_pin</c> da ponte; a casca o usa no menu técnico nativo. Fonte de verdade única = servidor (painel);
/// o hash do instalador (<c>config.json → kiosk.tech_pin_hash</c>) só vale até o primeiro PIN do servidor.
/// Algoritmo idêntico ao <see cref="PinHash"/> (PBKDF2-HMAC-SHA256, UTF-8, 32 bytes); muda só a codificação (base64url).
/// </summary>
public static class ServerTechPin
{
    public const int MinIterations = 100_000;
    public const int MaxIterations = 5_000_000;

    public static bool TryConvert(JsonObject? p, out string? encoded, out long setAtMs, out string? error)
    {
        encoded = null;
        setAtMs = 0;
        error = null;
        if (p is null) { error = "registro ausente"; return false; }
        if (p.ContainsKey("pin")) { error = "PIN em claro não é aceito"; return false; }
        if (Str(p, "algo") != "pbkdf2-sha256") { error = "algoritmo não suportado"; return false; }
        if (!TryLong(p["iterations"], out var iter) || iter < MinIterations || iter > MaxIterations) { error = "iterations fora do intervalo"; return false; }
        if (!Base64Url.TryDecode(Str(p, "salt_b64u"), out var salt) || salt.Length < 16 || salt.Length > 64) { error = "salt inválido"; return false; }
        if (!Base64Url.TryDecode(Str(p, "hash_b64u"), out var hash) || hash.Length != 32) { error = "hash inválido"; return false; }
        setAtMs = TryLong(p["set_at_ms"], out var s) && s > 0 ? s : 0;
        encoded = $"{PinHash.Prefix}${iter}${Convert.ToBase64String(salt)}${Convert.ToBase64String(hash)}";
        return true;
    }

    private static string? Str(JsonObject o, string name) =>
        o[name] is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;

    private static bool TryLong(JsonNode? n, out long value)
    {
        value = 0;
        if (n is not JsonValue v) return false;
        if (v.TryGetValue<long>(out value)) return true;
        if (v.TryGetValue<int>(out var i)) { value = i; return true; }
        if (v.TryGetValue<JsonElement>(out var e) && e.ValueKind == JsonValueKind.Number && e.TryGetInt64(out value)) return true;
        return false;
    }
}

/// <summary>Escolha do PIN efetivo do menu técnico nativo.</summary>
public static class TechPinResolver
{
    public enum Source { None, Installer, Server }

    /// <summary>Servidor (repassado pelo bundle) tem precedência; o PIN do instalador é só a semente inicial.</summary>
    public static (string? Hash, Source From) Effective(string? serverHash, string? installerHash)
    {
        if (!string.IsNullOrEmpty(serverHash) && PinHash.IsValidFormat(serverHash)) return (serverHash, Source.Server);
        if (!string.IsNullOrEmpty(installerHash) && PinHash.IsValidFormat(installerHash)) return (installerHash, Source.Installer);
        return (null, Source.None);
    }

    /// <summary>Aceita um registro novo se não houver anterior ou se não for mais antigo (proteção contra reenvio de registro velho).</summary>
    public static bool ShouldAccept(long? currentSetAtMs, long incomingSetAtMs, string? currentHash, string incomingHash)
    {
        if (string.Equals(currentHash, incomingHash, StringComparison.Ordinal)) return false; // nada muda
        if (currentSetAtMs is long cur && incomingSetAtMs > 0 && incomingSetAtMs < cur) return false;
        return true;
    }
}
