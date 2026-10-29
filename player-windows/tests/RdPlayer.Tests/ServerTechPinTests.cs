using System.Text.Json.Nodes;
using RdPlayer.Core.Security;

namespace RdPlayer.Tests;

/// <summary>PIN técnico do servidor (set_tech_pin) repassado pelo bundle à casca: fonte de verdade única.</summary>
public class ServerTechPinTests
{
    // vetor gerado com o MESMO algoritmo do backend (backend/app/modules/playback/commands.py:tech_pin_args):
    // Python hashlib.pbkdf2_hmac('sha256', '739154', bytes(16..31), 210000, dklen=32), base64url sem padding.
    private const string Salt = "EBESExQVFhcYGRobHB0eHw";
    private const string Hash = "eT3QxwsoWhZv8ZNt9y3OoaR_D6quMwV8XJSPsQyiXWI";

    private static JsonObject Rec(string? salt = Salt, string? hash = Hash, int iterations = 210000, string algo = "pbkdf2-sha256") => new()
    {
        ["algo"] = algo,
        ["iterations"] = iterations,
        ["salt_b64u"] = salt,
        ["hash_b64u"] = hash,
        ["set_at_ms"] = 1791380000000,
    };

    [Fact]
    public void Backend_vector_converts_and_verifies_with_native_pin_hash()
    {
        Assert.True(ServerTechPin.TryConvert(Rec(), out var enc, out var setAt, out var err), err);
        Assert.Equal(1791380000000, setAt);
        Assert.StartsWith("pbkdf2-sha256$210000$", enc);
        Assert.True(PinHash.Verify("739154", enc));
        Assert.False(PinHash.Verify("739155", enc));
    }

    [Theory]
    [InlineData("sha1", 210000, Salt, Hash)]
    [InlineData("pbkdf2-sha256", 1000, Salt, Hash)]          // iterações abaixo do mínimo do protocolo
    [InlineData("pbkdf2-sha256", 210000, "AAAA", Hash)]      // sal curto
    [InlineData("pbkdf2-sha256", 210000, Salt, "AAAA")]      // hash ≠ 32 bytes
    [InlineData("pbkdf2-sha256", 210000, Salt, "eT3Q+wso")]  // não é base64url
    public void Invalid_records_rejected(string algo, int iter, string salt, string hash)
    {
        Assert.False(ServerTechPin.TryConvert(Rec(salt, hash, iter, algo), out _, out _, out _));
    }

    [Fact]
    public void Plaintext_pin_never_accepted()
    {
        var r = Rec();
        r["pin"] = "739154";
        Assert.False(ServerTechPin.TryConvert(r, out _, out _, out var err));
        Assert.Contains("claro", err);
        Assert.False(ServerTechPin.TryConvert(null, out _, out _, out _));
    }

    [Fact]
    public void Server_pin_takes_precedence_over_installer_seed()
    {
        ServerTechPin.TryConvert(Rec(), out var server, out _, out _);
        var installer = PinHash.Create("111111", 1000);
        Assert.Equal((server, TechPinResolver.Source.Server), TechPinResolver.Effective(server, installer));
        Assert.Equal((installer, TechPinResolver.Source.Installer), TechPinResolver.Effective(null, installer));
        Assert.Equal(((string?)null, TechPinResolver.Source.None), TechPinResolver.Effective(null, null));
        Assert.Equal((installer, TechPinResolver.Source.Installer), TechPinResolver.Effective("lixo", installer));
    }

    [Fact]
    public void Older_or_identical_records_ignored()
    {
        Assert.True(TechPinResolver.ShouldAccept(null, 10, null, "a"));
        Assert.False(TechPinResolver.ShouldAccept(10, 20, "a", "a"));   // mesmo hash
        Assert.False(TechPinResolver.ShouldAccept(20, 10, "a", "b"));   // mais antigo
        Assert.True(TechPinResolver.ShouldAccept(20, 30, "a", "b"));
        Assert.True(TechPinResolver.ShouldAccept(20, 0, "a", "b"));     // sem set_at_ms: aceita (comando novo)
    }
}
