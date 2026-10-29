using System.Text.Json.Nodes;
using RdPlayer.Core.Crypto;
using RdPlayer.Core.Json;

namespace RdPlayer.Tests;

public class CanonicalJsonTests
{
    [Fact]
    public void Sorts_keys_and_removes_whitespace()
    {
        Assert.Equal("{\"a\":1,\"b\":[true,false,null],\"c\":{\"x\":\"y\",\"z\":0}}",
            Rdj1.Canonicalize("{ \"c\": {\"z\": 0, \"x\": \"y\"}, \"b\": [true, false, null], \"a\": 1 }"));
    }

    [Fact]
    public void Escapes_minimally_and_keeps_non_ascii()
    {
        var node = new JsonObject { ["s"] = "aç\"\\\b\f\n\r\t\u0001/€😀" };
        Assert.Equal("{\"s\":\"aç\\\"\\\\\\b\\f\\n\\r\\t\\u0001/€😀\"}", Rdj1.Canonicalize(node));
    }

    [Theory]
    [InlineData("1.5")]
    [InlineData("1e3")]
    [InlineData("1.0")]
    [InlineData("{\"a\":2.0}")]
    public void Rejects_floats(string json)
    {
        Assert.Throws<CanonicalJsonException>(() => Rdj1.Canonicalize(json));
    }

    [Fact]
    public void Rejects_duplicate_keys()
    {
        Assert.Throws<CanonicalJsonException>(() => Rdj1.Canonicalize("{\"a\":1,\"a\":2}"));
    }

    [Fact]
    public void Large_integers_preserved_and_negative_zero_normalized()
    {
        Assert.Equal("[1791380000000,-5,0]", Rdj1.Canonicalize("[1791380000000, -5, -0]"));
    }

    [Fact]
    public void Orders_by_code_point_not_utf16()
    {
        // U+FF61 (BMP alto) < U+1F600 (astral) por ponto de código; em UTF-16 a ordem seria inversa.
        var node = new JsonObject { ["😀"] = 1, ["｡"] = 2 };
        Assert.Equal("{\"｡\":2,\"😀\":1}", Rdj1.Canonicalize(node));
    }

    [Fact]
    public void Auth_message_example_matches_rdpp()
    {
        var o = new JsonObject { ["purpose"] = "rd-player-auth-v1", ["device_id"] = "d", ["challenge"] = "c" };
        Assert.Equal("{\"challenge\":\"c\",\"device_id\":\"d\",\"purpose\":\"rd-player-auth-v1\"}", Rdj1.Canonicalize(o));
    }

    [Theory]
    [InlineData(new byte[] { }, "")]
    [InlineData(new byte[] { 0xfb, 0xff }, "-_8")]
    [InlineData(new byte[] { 1, 2, 3 }, "AQID")]
    public void Base64Url_roundtrip(byte[] data, string expected)
    {
        Assert.Equal(expected, Base64Url.Encode(data));
        Assert.Equal(data, Base64Url.Decode(expected));
    }

    [Theory]
    [InlineData("AQID=")]
    [InlineData("A+ID")]
    [InlineData("A")]
    [InlineData("AQJ")] // bits sobrando (não canônico)
    public void Base64Url_rejects_invalid(string s)
    {
        Assert.False(Base64Url.TryDecode(s, out _));
    }
}
