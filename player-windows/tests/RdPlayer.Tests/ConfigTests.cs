using System.Security.Cryptography;
using RdPlayer.Core.Config;
using RdPlayer.Core.Crypto;
using RdPlayer.Core.Security;

namespace RdPlayer.Tests;

public class ConfigTests
{
    private const string Minimal = "{\"api_base_url\":\"https://api.exemplo.com.br\"}";

    [Fact]
    public void Minimal_config_valid_with_defaults()
    {
        var r = ConfigLoader.Parse(Minimal);
        Assert.True(r.Ok, string.Join("; ", r.Errors));
        var c = r.Config;
        Assert.Equal("local", c.Web.Source);
        Assert.Equal("player.rd.local", c.Web.VirtualHost);
        Assert.True(c.Display.Fullscreen);
        Assert.False(c.Autostart.Enabled); // desligado por padrão
        Assert.True(c.Power.PreventSleep);
        Assert.Equal(30000, c.Watchdog.ProgressTimeoutMs);
        Assert.Equal("wss://api.exemplo.com.br", ConfigLoader.EffectiveWsBaseUrl(c));
        Assert.Contains(r.Warnings, w => w.Contains("tech_pin_hash"));
        Assert.Contains(r.Warnings, w => w.Contains("public_keys"));
    }

    [Fact]
    public void Missing_file_reports_error()
    {
        var r = ConfigLoader.Load(Path.Combine(Path.GetTempPath(), "nao-existe-" + Guid.NewGuid().ToString("N"), "config.json"));
        Assert.False(r.Ok);
        Assert.False(r.Exists);
        Assert.StartsWith("CONFIG_MISSING", r.Errors[0]);
    }

    [Theory]
    [InlineData("{}", "api_base_url obrigatório")]
    [InlineData("{\"api_base_url\":\"http://api.exemplo.com.br\"}", "api_base_url inválido")]
    [InlineData("{\"api_base_url\":\"https://user:pw@api.exemplo.com.br\"}", "api_base_url inválido")]
    [InlineData("{\"api_base_url\":\"https://api.exemplo.com.br?x=1\"}", "api_base_url inválido")]
    [InlineData("{\"api_base_url\":\"https://a.b\",\"ws_base_url\":\"ws://a.b\"}", "ws_base_url inválido")]
    [InlineData("{\"api_base_url\":\"https://a.b\",\"web\":{\"source\":\"ftp\"}}", "web.source")]
    [InlineData("{\"api_base_url\":\"https://a.b\",\"web\":{\"source\":\"remote\"}}", "web.remote_url")]
    [InlineData("{\"api_base_url\":\"https://a.b\",\"web\":{\"virtual_host\":\"localhost\"}}", "web.virtual_host")]
    [InlineData("{\"api_base_url\":\"https://a.b\",\"web\":{\"entry\":\"../x.html\"}}", "web.entry")]
    [InlineData("{\"api_base_url\":\"https://a.b\",\"display\":{\"monitor\":\"tv\"}}", "display.monitor")]
    [InlineData("{\"api_base_url\":\"https://a.b\",\"kiosk\":{\"tech_shortcut\":\"Q\"}}", "kiosk.tech_shortcut")]
    [InlineData("{\"api_base_url\":\"https://a.b\",\"kiosk\":{\"tech_pin_hash\":\"1234\"}}", "kiosk.tech_pin_hash")]
    [InlineData("{\"api_base_url\":\"https://a.b\",\"watchdog\":{\"hang_timeout_ms\":5000}}", "hang_timeout_ms")]
    [InlineData("{\"api_base_url\":\"https://a.b\",\"autostart\":{\"method\":\"service\"}}", "autostart.method")]
    [InlineData("{\"api_base_url\":\"https://a.b\",\"update\":{\"public_keys\":[{\"key_id\":\"k\",\"public_key\":\"AQID\"}]}}", "update.public_keys")]
    [InlineData("{\"api_base_url\":\"https://a.b\",\"power\":{\"reboot_min_interval_minutes\":1}}", "reboot_min_interval_minutes")]
    public void Invalid_values_rejected(string json, string expectedFragment)
    {
        var r = ConfigLoader.Parse(json);
        Assert.False(r.Ok);
        Assert.Contains(r.Errors, e => e.Contains(expectedFragment));
    }

    [Fact]
    public void Invalid_json_reported_without_crash()
    {
        var r = ConfigLoader.Parse("{\"api_base_url\": ");
        Assert.False(r.Ok);
        Assert.StartsWith("CONFIG_INVALID_JSON", r.Errors[0]);
    }

    [Fact]
    public void Comments_trailing_commas_and_insecure_dev()
    {
        var r = ConfigLoader.Parse("{ // comentário\n \"api_base_url\":\"http://localhost:8000\", \"dev\":{\"allow_insecure_api\":true}, }");
        Assert.True(r.Ok, string.Join("; ", r.Errors));
        Assert.Equal("ws://localhost:8000", ConfigLoader.EffectiveWsBaseUrl(r.Config));
        Assert.Contains(r.Warnings, w => w.Contains("allow_insecure_api"));
    }

    [Fact]
    public void Full_config_with_keys_and_pin_valid()
    {
        using var k = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        var spki = Base64Url.Encode(k.ExportSubjectPublicKeyInfo());
        var pin = PinHash.Create("482913", iterations: 1000);
        var json = $$"""
        {
          "api_base_url": "https://api.exemplo.com.br/",
          "ws_base_url": "wss://ws.exemplo.com.br",
          "panel_url": "https://painel.exemplo.com.br",
          "display": { "monitor": "\\\\.\\DISPLAY2", "always_on_top": false },
          "kiosk": { "tech_pin_hash": "{{pin}}", "tech_shortcut": "Ctrl+Shift+F10" },
          "update": { "public_keys": [ { "key_id": "rel-1", "public_key": "{{spki}}" } ] },
          "autostart": { "enabled": true, "method": "run" }
        }
        """;
        var r = ConfigLoader.Parse(json);
        Assert.True(r.Ok, string.Join("; ", r.Errors));
        Assert.Equal(@"\\.\DISPLAY2", r.Config.Display.Monitor);
        Assert.False(r.Config.Display.AlwaysOnTop);
        Assert.Equal("wss://ws.exemplo.com.br", ConfigLoader.EffectiveWsBaseUrl(r.Config));
        Assert.Single(r.Config.Update.PublicKeys);
        Assert.DoesNotContain(r.Warnings, w => w.Contains("tech_pin_hash"));
    }

    [Fact]
    public void Null_sections_tolerated()
    {
        var r = ConfigLoader.Parse("{\"api_base_url\":\"https://a.b\",\"web\":null,\"update\":{\"public_keys\":null}}");
        Assert.True(r.Ok, string.Join("; ", r.Errors));
        Assert.NotNull(r.Config.Web);
    }

    [Fact]
    public void Shipped_example_config_is_valid()
    {
        // tests/RdPlayer.Tests/bin/<cfg>/net8.0 → raiz da solução
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null && !File.Exists(Path.Combine(dir.FullName, "Directory.Build.props"))) dir = dir.Parent;
        Assert.NotNull(dir);
        var r = ConfigLoader.Load(Path.Combine(dir!.FullName, "packaging", "config.example.json"));
        Assert.True(r.Ok, string.Join("; ", r.Errors));
        Assert.False(r.Config.Autostart.Enabled);
        Assert.Equal("player.rd.local", r.Config.Web.VirtualHost);
    }

    [Fact]
    public void Paths_resolve_overrides()
    {
        var p = PlayerPaths.Resolve("/tmp/x/config.json", "/tmp/data");
        Assert.EndsWith("config.json", p.ConfigPath);
        Assert.EndsWith(Path.Combine("data", "WebView2"), p.WebView2Dir);
        Assert.EndsWith(Path.Combine("state", "update_state.json"), p.UpdateStatePath);
    }
}
