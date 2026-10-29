namespace RdPlayer.Core.Crypto;

/// <summary>base64url sem padding (RDPP/1 §0).</summary>
public static class Base64Url
{
    public static string Encode(ReadOnlySpan<byte> data) =>
        Convert.ToBase64String(data).TrimEnd('=').Replace('+', '-').Replace('/', '_');

    public static byte[] Decode(string s)
    {
        if (!TryDecode(s, out var bytes)) throw new FormatException("base64url inválido");
        return bytes;
    }

    public static bool TryDecode(string? s, out byte[] bytes)
    {
        bytes = Array.Empty<byte>();
        if (s is null) return false;
        foreach (char c in s)
        {
            bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' || c == '_';
            if (!ok) return false;
        }
        if (s.Length % 4 == 1) return false;
        var t = s.Replace('-', '+').Replace('_', '/');
        t = t.PadRight(t.Length + (4 - t.Length % 4) % 4, '=');
        try
        {
            bytes = Convert.FromBase64String(t);
            // rejeita codificações não canônicas (bits sobrando)
            return Encode(bytes) == s;
        }
        catch (FormatException)
        {
            return false;
        }
    }
}
