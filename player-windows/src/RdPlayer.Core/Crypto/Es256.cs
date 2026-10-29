using System.Security.Cryptography;

namespace RdPlayer.Core.Crypto;

/// <summary>ES256 = ECDSA P-256 + SHA-256; assinatura bruta r‖s (64 bytes); chave pública SPKI DER (RDPP/1 §0).</summary>
public static class Es256
{
    private const string P256Oid = "1.2.840.10045.3.1.7";

    public static bool IsP256Spki(ReadOnlySpan<byte> spki)
    {
        try
        {
            using var ec = ECDsa.Create();
            ec.ImportSubjectPublicKeyInfo(spki, out int read);
            return read == spki.Length && IsP256(ec);
        }
        catch (CryptographicException)
        {
            return false;
        }
    }

    public static bool Verify(ReadOnlySpan<byte> spki, ReadOnlySpan<byte> message, ReadOnlySpan<byte> signatureRaw)
    {
        if (signatureRaw.Length != 64) return false;
        try
        {
            using var ec = ECDsa.Create();
            ec.ImportSubjectPublicKeyInfo(spki, out int read);
            if (read != spki.Length || !IsP256(ec)) return false;
            return ec.VerifyData(message, signatureRaw, HashAlgorithmName.SHA256, DSASignatureFormat.IeeeP1363FixedFieldConcatenation);
        }
        catch (CryptographicException)
        {
            return false;
        }
    }

    private static bool IsP256(ECDsa ec)
    {
        var p = ec.ExportParameters(false);
        if (!p.Curve.IsNamed) return false;
        var oid = p.Curve.Oid;
        if (oid.Value == P256Oid) return true;
        var fn = oid.FriendlyName ?? "";
        return fn.Equals("nistP256", StringComparison.OrdinalIgnoreCase)
            || fn.Equals("ECDSA_P256", StringComparison.OrdinalIgnoreCase)
            || fn.Equals("prime256v1", StringComparison.OrdinalIgnoreCase)
            || fn.Equals("secp256r1", StringComparison.OrdinalIgnoreCase);
    }
}
