using System.Security.Cryptography;
using System.Text.Json.Nodes;
using RdPlayer.Core.Crypto;
using RdPlayer.Core.Json;

namespace RdPlayer.Core.Update;

public sealed record UpdatePublicKey(string KeyId, string PublicKey);

public enum UpdateVerdict
{
    Ok,
    KeysNotConfigured,
    UnknownKey,
    SignatureInvalid,
    BadCurrentVersion,
    SameVersion,
    Downgrade,
    BlacklistedVersion,
}

/// <summary>
/// Verificação do pacote de atualização (RDPP/1 §8):
/// assinatura ES256 da chave de release (SPKI fixado em config.json) sobre
/// RDJ1 {"platform","purpose":"rd-app-update-v1","sha256","size","version"}; depois sha256/tamanho do arquivo.
/// </summary>
public sealed class UpdateVerifier
{
    public const string Purpose = "rd-app-update-v1";
    private readonly List<(string KeyId, byte[] Spki)> _keys = new();
    private readonly string _platform;

    public UpdateVerifier(IEnumerable<UpdatePublicKey> keys, string platform = "windows")
    {
        _platform = platform;
        foreach (var k in keys)
        {
            if (Base64Url.TryDecode(k.PublicKey, out var spki) && Es256.IsP256Spki(spki))
                _keys.Add((k.KeyId ?? "", spki));
        }
    }

    public int UsableKeyCount => _keys.Count;

    public static byte[] BuildSignedMessage(string platform, AppUpdateInfo info)
    {
        var o = new JsonObject
        {
            ["platform"] = platform,
            ["purpose"] = Purpose,
            ["sha256"] = info.Sha256,
            ["size"] = info.Size,
            ["version"] = info.Version,
        };
        return Rdj1.CanonicalBytes(o);
    }

    public UpdateVerdict VerifySignature(AppUpdateInfo info)
    {
        if (_keys.Count == 0) return UpdateVerdict.KeysNotConfigured;
        if (!Base64Url.TryDecode(info.Signature, out var sig) || sig.Length != 64) return UpdateVerdict.SignatureInvalid;
        var msg = BuildSignedMessage(_platform, info);
        var candidates = info.KeyId is { Length: > 0 } kid ? _keys.Where(k => k.KeyId == kid).ToList() : _keys;
        if (candidates.Count == 0) return UpdateVerdict.UnknownKey;
        foreach (var k in candidates)
            if (Es256.Verify(k.Spki, msg, sig)) return UpdateVerdict.Ok;
        return UpdateVerdict.SignatureInvalid;
    }

    /// <summary>Assinatura primeiro; depois política de versão.</summary>
    public UpdateVerdict Evaluate(AppUpdateInfo info, string currentVersion, bool allowDowngrade, IReadOnlyCollection<string> badVersions)
    {
        var sig = VerifySignature(info);
        if (sig != UpdateVerdict.Ok) return sig;
        if (!AppVersion.TryParse(currentVersion, out var cur)) return UpdateVerdict.BadCurrentVersion;
        var next = AppVersion.Parse(info.Version);
        if (badVersions.Contains(info.Version)) return UpdateVerdict.BlacklistedVersion;
        int c = next.CompareTo(cur);
        if (c == 0) return UpdateVerdict.SameVersion;
        if (c < 0 && !allowDowngrade) return UpdateVerdict.Downgrade;
        return UpdateVerdict.Ok;
    }

    public static async Task<(bool Ok, string ActualSha256, long ActualSize)> VerifyFileAsync(string path, string expectedSha256, long expectedSize, CancellationToken ct = default)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        long total = 0;
        var buf = new byte[81920];
        await using (var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, buf.Length, useAsync: true))
        {
            int n;
            while ((n = await fs.ReadAsync(buf, ct).ConfigureAwait(false)) > 0)
            {
                hash.AppendData(buf, 0, n);
                total += n;
            }
        }
        var actual = Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
        return (total == expectedSize && actual == expectedSha256, actual, total);
    }
}
