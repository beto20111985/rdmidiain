using System.Security.Cryptography;
using System.Text;
using RdPlayer.Core.Crypto;
using RdPlayer.Core.Update;

namespace RdPlayer.Tests;

public class UpdateVerifierTests
{
    private static (ECDsa Key, string Spki) NewKey()
    {
        var k = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        return (k, Base64Url.Encode(k.ExportSubjectPublicKeyInfo()));
    }

    private static AppUpdateInfo Signed(ECDsa key, string version = "1.2.0", string sha = null!, long size = 1234, string? keyId = "rel-1", string platform = "windows")
    {
        sha ??= new string('a', 64);
        var unsigned = new AppUpdateInfo(version, "https://cdn.exemplo/pkg.zip", sha, size, "", keyId, false);
        var msg = UpdateVerifier.BuildSignedMessage(platform, unsigned);
        var sig = key.SignData(msg, HashAlgorithmName.SHA256, DSASignatureFormat.IeeeP1363FixedFieldConcatenation);
        return unsigned with { Signature = Base64Url.Encode(sig) };
    }

    [Fact]
    public void Signed_message_is_canonical_rdj1()
    {
        var info = new AppUpdateInfo("1.2.0", "https://x/y.zip", new string('b', 64), 42, "", null, true);
        var text = Encoding.UTF8.GetString(UpdateVerifier.BuildSignedMessage("windows", info));
        Assert.Equal("{\"platform\":\"windows\",\"purpose\":\"rd-app-update-v1\",\"sha256\":\"" + new string('b', 64) + "\",\"size\":42,\"version\":\"1.2.0\"}", text);
    }

    [Fact]
    public void Valid_signature_accepted_and_newer_version_ok()
    {
        var (key, spki) = NewKey();
        var v = new UpdateVerifier(new[] { new UpdatePublicKey("rel-1", spki) });
        var info = Signed(key);
        Assert.Equal(UpdateVerdict.Ok, v.VerifySignature(info));
        Assert.Equal(UpdateVerdict.Ok, v.Evaluate(info, "1.0.0", false, Array.Empty<string>()));
    }

    [Fact]
    public void Tampered_fields_rejected()
    {
        var (key, spki) = NewKey();
        var v = new UpdateVerifier(new[] { new UpdatePublicKey("rel-1", spki) });
        var info = Signed(key);
        Assert.Equal(UpdateVerdict.SignatureInvalid, v.VerifySignature(info with { Sha256 = new string('c', 64) }));
        Assert.Equal(UpdateVerdict.SignatureInvalid, v.VerifySignature(info with { Size = 1235 }));
        Assert.Equal(UpdateVerdict.SignatureInvalid, v.VerifySignature(info with { Version = "9.9.9" }));
        // url fora da assinatura (pode ser de CDN)
        Assert.Equal(UpdateVerdict.Ok, v.VerifySignature(info with { Url = "https://outro/pkg.zip" }));
    }

    [Fact]
    public void Signature_for_other_platform_rejected()
    {
        var (key, spki) = NewKey();
        var v = new UpdateVerifier(new[] { new UpdatePublicKey("rel-1", spki) });
        Assert.Equal(UpdateVerdict.SignatureInvalid, v.VerifySignature(Signed(key, platform: "android")));
    }

    [Fact]
    public void Wrong_key_unknown_key_and_no_keys()
    {
        var (key, _) = NewKey();
        var (_, otherSpki) = NewKey();
        var info = Signed(key);
        Assert.Equal(UpdateVerdict.SignatureInvalid, new UpdateVerifier(new[] { new UpdatePublicKey("rel-1", otherSpki) }).VerifySignature(info));
        Assert.Equal(UpdateVerdict.UnknownKey, new UpdateVerifier(new[] { new UpdatePublicKey("rel-2", otherSpki) }).VerifySignature(info));
        Assert.Equal(UpdateVerdict.KeysNotConfigured, new UpdateVerifier(Array.Empty<UpdatePublicKey>()).VerifySignature(info));
    }

    [Fact]
    public void Without_key_id_tries_all_keys()
    {
        var (key, spki) = NewKey();
        var (_, otherSpki) = NewKey();
        var v = new UpdateVerifier(new[] { new UpdatePublicKey("a", otherSpki), new UpdatePublicKey("b", spki) });
        Assert.Equal(UpdateVerdict.Ok, v.VerifySignature(Signed(key, keyId: null)));
    }

    [Fact]
    public void Non_p256_keys_ignored()
    {
        using var k1 = ECDsa.Create(ECCurve.NamedCurves.nistP384);
        var v = new UpdateVerifier(new[] { new UpdatePublicKey("x", Base64Url.Encode(k1.ExportSubjectPublicKeyInfo())), new UpdatePublicKey("y", "lixo") });
        Assert.Equal(0, v.UsableKeyCount);
        Assert.False(Es256.IsP256Spki(k1.ExportSubjectPublicKeyInfo()));
    }

    [Fact]
    public void Der_signature_format_rejected()
    {
        var (key, spki) = NewKey();
        var info = Signed(key);
        var der = key.SignData(UpdateVerifier.BuildSignedMessage("windows", info), HashAlgorithmName.SHA256, DSASignatureFormat.Rfc3279DerSequence);
        var v = new UpdateVerifier(new[] { new UpdatePublicKey("rel-1", spki) });
        Assert.Equal(UpdateVerdict.SignatureInvalid, v.VerifySignature(info with { Signature = Base64Url.Encode(der) }));
    }

    [Fact]
    public void Version_policy()
    {
        var (key, spki) = NewKey();
        var v = new UpdateVerifier(new[] { new UpdatePublicKey("rel-1", spki) });
        Assert.Equal(UpdateVerdict.SameVersion, v.Evaluate(Signed(key, "1.0.0"), "1.0.0", false, Array.Empty<string>()));
        Assert.Equal(UpdateVerdict.Downgrade, v.Evaluate(Signed(key, "0.9.0"), "1.0.0", false, Array.Empty<string>()));
        Assert.Equal(UpdateVerdict.Ok, v.Evaluate(Signed(key, "0.9.0"), "1.0.0", true, Array.Empty<string>()));
        Assert.Equal(UpdateVerdict.BlacklistedVersion, v.Evaluate(Signed(key, "1.1.0"), "1.0.0", false, new[] { "1.1.0" }));
        Assert.Equal(UpdateVerdict.Ok, v.Evaluate(Signed(key, "1.1.0-beta.2"), "1.1.0-beta.1", false, Array.Empty<string>()));
        Assert.Equal(UpdateVerdict.Downgrade, v.Evaluate(Signed(key, "1.1.0-beta.2"), "1.1.0", false, Array.Empty<string>()));
    }

    [Fact]
    public async Task File_hash_and_size_checked()
    {
        var path = Path.Combine(Path.GetTempPath(), "rdpkg-" + Guid.NewGuid().ToString("N"));
        var data = Encoding.UTF8.GetBytes("pacote de teste RD");
        await File.WriteAllBytesAsync(path, data);
        try
        {
            var sha = Convert.ToHexString(SHA256.HashData(data)).ToLowerInvariant();
            Assert.True((await UpdateVerifier.VerifyFileAsync(path, sha, data.Length)).Ok);
            Assert.False((await UpdateVerifier.VerifyFileAsync(path, sha, data.Length + 1)).Ok);
            Assert.False((await UpdateVerifier.VerifyFileAsync(path, new string('0', 64), data.Length)).Ok);
        }
        finally
        {
            File.Delete(path);
        }
    }

    [Theory]
    [InlineData("{\"version\":\"1.0.1\",\"url\":\"https://a/b.zip\",\"sha256\":\"" + "abababababababababababababababababababababababababababababababab" + "\",\"size\":10,\"signature\":\"AQID\",\"mandatory\":false}", true)]
    [InlineData("{\"version\":\"1.0\",\"url\":\"https://a/b.zip\",\"sha256\":\"ab\",\"size\":10,\"signature\":\"AQID\"}", false)]
    [InlineData("{\"version\":\"1.0.1\",\"url\":\"https://a/b.zip\",\"sha256\":\"" + "ABABABABABABABABABABABABABABABABABABABABABABABABABABABABABABABAB" + "\",\"size\":10,\"signature\":\"AQID\"}", false)]
    [InlineData("{\"version\":\"1.0.1\",\"url\":\"https://a/b.zip\",\"sha256\":\"" + "abababababababababababababababababababababababababababababababab" + "\",\"size\":10.5,\"signature\":\"AQID\"}", false)]
    [InlineData("{\"version\":\"1.0.1\",\"url\":\"https://a/b.zip\",\"sha256\":\"" + "abababababababababababababababababababababababababababababababab" + "\",\"size\":-1,\"signature\":\"AQID\"}", false)]
    [InlineData("[]", false)]
    public void Parse_update_info(string json, bool ok)
    {
        Assert.Equal(ok, AppUpdateInfo.TryParse(json, out _, out _));
    }

    [Fact]
    public void AppVersion_ordering()
    {
        Assert.True(AppVersion.Parse("1.10.0") > AppVersion.Parse("1.9.9"));
        Assert.True(AppVersion.Parse("1.0.0") > AppVersion.Parse("1.0.0-rc.1"));
        Assert.True(AppVersion.Parse("1.0.0-rc.10") > AppVersion.Parse("1.0.0-rc.2"));
        Assert.True(AppVersion.Parse("1.0.0-beta") > AppVersion.Parse("1.0.0-alpha.9"));
        Assert.False(AppVersion.TryParse("01.0.0", out _));
        Assert.False(AppVersion.TryParse("1.0", out _));
        Assert.False(AppVersion.TryParse("../1.0.0", out _));
    }
}
