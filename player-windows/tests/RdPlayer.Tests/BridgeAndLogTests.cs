using System.Text.Json.Nodes;
using RdPlayer.Core;
using RdPlayer.Core.Bridge;
using RdPlayer.Core.Logging;

namespace RdPlayer.Tests;

public class BridgeAndLogTests
{
    [Fact]
    public void Parses_request_and_notify()
    {
        Assert.True(BridgeProtocol.TryParse("{\"rd\":1,\"kind\":\"request\",\"id\":\"a1-b2\",\"method\":\"get_telemetry\",\"params\":{}}", out var m, out _));
        Assert.Equal(BridgeKind.Request, m!.Kind);
        Assert.Equal("a1-b2", m.Id);
        Assert.Equal("get_telemetry", m.Method);

        Assert.True(BridgeProtocol.TryParse("{\"rd\":1,\"kind\":\"notify\",\"method\":\"progress\",\"params\":{\"state\":\"PLAYING\",\"creative_code\":\"RD-A7M2P9\"}}", out var n, out _));
        Assert.Equal(BridgeKind.Notify, n!.Kind);
        Assert.Equal("PLAYING", BridgeProtocol.Str(n.Params, "state"));

        Assert.True(BridgeProtocol.TryParse("{\"rd\":1,\"kind\":\"notify\",\"method\":\"ready\"}", out var r, out _));
        Assert.Empty(r!.Params);
    }

    [Theory]
    [InlineData("{\"rd\":2,\"kind\":\"request\",\"id\":\"1\",\"method\":\"get_info\"}")]
    [InlineData("{\"kind\":\"request\",\"id\":\"1\",\"method\":\"get_info\"}")]
    [InlineData("{\"rd\":1,\"kind\":\"request\",\"method\":\"get_info\"}")]
    [InlineData("{\"rd\":1,\"kind\":\"request\",\"id\":\"<script>\",\"method\":\"get_info\"}")]
    [InlineData("{\"rd\":1,\"kind\":\"request\",\"id\":\"1\",\"method\":\"Get-Info\"}")]
    [InlineData("{\"rd\":1,\"kind\":\"event\",\"id\":\"1\",\"method\":\"x\"}")]
    [InlineData("{\"rd\":1,\"kind\":\"request\",\"id\":\"1\",\"method\":\"get_info\",\"params\":[1]}")]
    [InlineData("\"texto\"")]
    [InlineData("{quebrado")]
    public void Rejects_malformed(string json)
    {
        Assert.False(BridgeProtocol.TryParse(json, out _, out var err));
        Assert.NotNull(err);
    }

    [Fact]
    public void Rejects_oversized()
    {
        var big = "{\"rd\":1,\"kind\":\"notify\",\"method\":\"log\",\"params\":{\"message\":\"" + new string('x', 70_000) + "\"}}";
        Assert.False(BridgeProtocol.TryParse(big, out _, out _));
    }

    [Fact]
    public void Builds_envelopes()
    {
        var ok = JsonNode.Parse(BridgeProtocol.Response("7", new JsonObject { ["status"] = "DONE" }))!;
        Assert.Equal("response", (string?)ok["kind"]);
        Assert.True((bool)ok["ok"]!);
        Assert.Equal("DONE", (string?)ok["result"]!["status"]);
        var err = JsonNode.Parse(BridgeProtocol.Error("7", "UNSUPPORTED", "x"))!;
        Assert.False((bool)err["ok"]!);
        Assert.Equal("UNSUPPORTED", (string?)err["error"]!["code"]);
        var ev = JsonNode.Parse(BridgeProtocol.Event("display_changed", new JsonObject { ["orientation"] = "PORTRAIT" }))!;
        Assert.Equal("event", (string?)ev["kind"]);
        Assert.Equal(1, (int)ev["rd"]!);
    }

    [Fact]
    public void Injected_script_is_safe_against_breakout()
    {
        var info = new JsonObject { ["platform"] = "windows", ["config"] = new JsonObject { ["api_base_url"] = "https://x/</script><script>alert(1)</script>'\"" } };
        var js = BridgeProtocol.BuildInjectedScript(info, hideCursor: true);
        Assert.StartsWith("(()=>{", js);
        Assert.DoesNotContain("</script>", js);
        Assert.DoesNotContain("'\"", js);
        Assert.Contains("RD_NATIVE", js);
        Assert.Contains("cursor:none", js);
        Assert.DoesNotContain("cursor:none", BridgeProtocol.BuildInjectedScript(info, hideCursor: false));
    }

    [Theory]
    [InlineData("Authorization: Bearer abc.def-ghi", "Bearer ***")]
    [InlineData("token eyJhbGciOiJFUzI1NiJ9.eyJzdWIiOiJkZXYifQ.c2lnbmF0dXJl fim", "***jwt***")]
    [InlineData("{\"access_token\":\"segredo\",\"x\":1}", "\"access_token\":\"***\"")]
    [InlineData("{\"password\" : \"123456\"}", "\"password\":\"***\"")]
    [InlineData("wss://h/ws/player?ticket=abc123&x=1", "?ticket=***&x=1")]
    public void Redacts_secrets(string input, string expectedFragment)
    {
        var r = LogRedactor.Redact(input);
        Assert.Contains(expectedFragment, r);
        Assert.DoesNotContain("segredo", r);
        Assert.DoesNotContain("123456", r);
        Assert.DoesNotContain("abc123", r);
    }

    [Fact]
    public void Log_rotates_and_keeps_max_files()
    {
        var dir = Path.Combine(Path.GetTempPath(), "rdlog-" + Guid.NewGuid().ToString("N"));
        try
        {
            using (var log = new RotatingFileLog(dir, "shell", LogLevel.Info, maxBytes: 16 * 1024, maxFiles: 3))
            {
                for (int i = 0; i < 2000; i++) log.Info("teste", $"linha {i} Bearer zzz {new string('x', 40)}");
                log.Debug("teste", "não deve aparecer");
            }
            var files = Directory.GetFiles(dir).Select(Path.GetFileName).OrderBy(x => x).ToArray();
            Assert.Equal(new[] { "shell.1.log", "shell.2.log", "shell.log" }, files);
            foreach (var f in Directory.GetFiles(dir))
            {
                Assert.True(new FileInfo(f).Length <= 16 * 1024);
                var text = File.ReadAllText(f);
                Assert.DoesNotContain("zzz", text);
                Assert.DoesNotContain("não deve aparecer", text);
            }
            Assert.Contains("linha 1999", File.ReadAllText(Path.Combine(dir, "shell.log")));
        }
        finally
        {
            Directory.Delete(dir, true);
        }
    }

    [Fact]
    public void Command_line_parsing()
    {
        var c = CommandLine.Parse(new[] { "--smoke-test", "--config", "c.json", "--data-root", "d", "--smoke-timeout", "20", "--supervised", "--xyz" });
        Assert.True(c.SmokeTest);
        Assert.True(c.Supervised);
        Assert.Equal("c.json", c.ConfigPath);
        Assert.Equal("d", c.DataRoot);
        Assert.Equal(20, c.SmokeTimeoutSeconds);
        Assert.Contains("--xyz", c.Unknown);
        var relaunch = c.ToShellArgs(supervised: true);
        Assert.DoesNotContain("--smoke-test", relaunch);
        Assert.Contains("--supervised", relaunch);
        Assert.Equal(new[] { "--supervised", "--config", "c.json", "--data-root", "d" }, relaunch);
        Assert.Equal(1234, CommandLine.Parse(new[] { "--takeover", "1234" }).TakeoverPid);
        Assert.Null(CommandLine.Parse(new[] { "--takeover", "x" }).TakeoverPid);
    }
}
