using System.Security.Cryptography;
using System.Text;

namespace RdPlayer.Core.Security;

/// <summary>
/// Hash do PIN técnico: <c>pbkdf2-sha256$&lt;iterações&gt;$&lt;sal base64&gt;$&lt;hash base64&gt;</c> (PBKDF2-HMAC-SHA256, 32 bytes, senha em UTF-8).
/// Compatível com <c>Rfc2898DeriveBytes(pin, sal, iter, SHA256)</c> do PowerShell 5.1 (install.ps1).
/// </summary>
public static class PinHash
{
    public const string Prefix = "pbkdf2-sha256";
    public const int DefaultIterations = 210_000;
    public const int MinVerifyIterations = 1_000;
    public const int MaxIterations = 5_000_000;

    public static bool IsAcceptablePin(string? pin) =>
        pin is not null && pin.Length is >= 4 and <= 32 && pin.All(c => !char.IsControl(c) && !char.IsWhiteSpace(c));

    public static string Create(string pin, int iterations = DefaultIterations, byte[]? salt = null)
    {
        if (!IsAcceptablePin(pin)) throw new ArgumentException("PIN deve ter de 4 a 32 caracteres sem espaços");
        salt ??= RandomNumberGenerator.GetBytes(16);
        var hash = Rfc2898DeriveBytes.Pbkdf2(Encoding.UTF8.GetBytes(pin), salt, iterations, HashAlgorithmName.SHA256, 32);
        return $"{Prefix}${iterations}${Convert.ToBase64String(salt)}${Convert.ToBase64String(hash)}";
    }

    public static bool IsValidFormat(string? encoded) => TryDecode(encoded, out _, out _, out _);

    public static bool Verify(string? pin, string? encoded)
    {
        if (pin is null || !TryDecode(encoded, out int iter, out var salt, out var expected)) return false;
        var actual = Rfc2898DeriveBytes.Pbkdf2(Encoding.UTF8.GetBytes(pin), salt, iter, HashAlgorithmName.SHA256, expected.Length);
        return CryptographicOperations.FixedTimeEquals(actual, expected);
    }

    private static bool TryDecode(string? encoded, out int iterations, out byte[] salt, out byte[] hash)
    {
        iterations = 0;
        salt = hash = Array.Empty<byte>();
        if (string.IsNullOrEmpty(encoded)) return false;
        var parts = encoded.Split('$');
        if (parts.Length != 4 || parts[0] != Prefix) return false;
        if (!int.TryParse(parts[1], out iterations) || iterations < MinVerifyIterations || iterations > MaxIterations) return false;
        try
        {
            salt = Convert.FromBase64String(parts[2]);
            hash = Convert.FromBase64String(parts[3]);
        }
        catch (FormatException)
        {
            return false;
        }
        return salt.Length >= 8 && hash.Length is >= 16 and <= 64;
    }
}

/// <summary>Limita tentativas de PIN: após N erros, bloqueia por um período.</summary>
public sealed class PinGuard
{
    private readonly int _maxAttempts;
    private readonly long _lockoutMs;
    private int _failures;
    private long _lockedUntilMs;

    public PinGuard(int maxAttempts, long lockoutMs)
    {
        _maxAttempts = Math.Max(1, maxAttempts);
        _lockoutMs = Math.Max(1000, lockoutMs);
    }

    public int Failures => _failures;

    public bool IsLocked(long nowMs, out long remainingMs)
    {
        remainingMs = Math.Max(0, _lockedUntilMs - nowMs);
        return remainingMs > 0;
    }

    /// <summary>Registra o resultado; retorna false se estiver bloqueado (tentativa ignorada).</summary>
    public bool Register(long nowMs, bool success)
    {
        if (IsLocked(nowMs, out _)) return false;
        if (success)
        {
            _failures = 0;
            return true;
        }
        _failures++;
        if (_failures >= _maxAttempts)
        {
            _lockedUntilMs = nowMs + _lockoutMs;
            _failures = 0;
        }
        return true;
    }
}
