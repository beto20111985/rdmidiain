using RdPlayer.Core.Telemetry;

namespace RdPlayer.Tests;

public class TelemetryTests
{
    private const string NetshEn = """

There is 1 interface on the system:

    Name                   : Wi-Fi
    Description            : Intel(R) Wi-Fi 6 AX201 160MHz
    GUID                   : 3f8c2c3b-1111-2222-3333-444455556666
    Physical address       : aa:bb:cc:dd:ee:ff
    State                  : connected
    SSID                   : Loja Centro
    BSSID                  : 11:22:33:44:55:66
    Network type           : Infrastructure
    Radio type             : 802.11ac
    Authentication         : WPA2-Personal
    Cipher                 : CCMP
    Connection mode        : Auto Connect
    Channel                : 44
    Receive rate (Mbps)    : 866.7
    Transmit rate (Mbps)   : 866.7
    Signal                 : 86%
    Profile                : Loja Centro

    Hosted network status  : Not available

""";

    private const string NetshPt = """

Há 1 interface no sistema:

    Nome                   : Wi-Fi
    Descrição              : Realtek RTL8821CE
    GUID                   : 0a0a0a0a-0000-0000-0000-000000000000
    Endereço físico        : aa:bb:cc:dd:ee:ff
    Estado                 : conectado
    SSID                   : Padaria: Wi-Fi Clientes
    BSSID                  : 11:22:33:44:55:66
    Tipo de rede           : Infraestrutura
    Tipo de rádio          : 802.11n
    Sinal                  : 40%
    Perfil                 : Padaria: Wi-Fi Clientes

""";

    private const string NetshWin11Rssi = """
    Name                   : Wi-Fi
    State                  : connected
    SSID                   : Mercado
    AP BSSID               : 11:22:33:44:55:66
    Signal                 : 99%
    Rssi                   : -41
""";

    private const string NetshDisconnected = """
    Name                   : Wi-Fi
    State                  : disconnected
    Radio status           : Hardware On
""";

    [Fact]
    public void Parses_english_output()
    {
        var w = NetshWlanParser.Parse(NetshEn);
        Assert.NotNull(w);
        Assert.Equal("Loja Centro", w!.Ssid);
        Assert.Equal(86, w.SignalPercent);
        Assert.Equal(-57, w.RssiDbm);
    }

    [Fact]
    public void Parses_portuguese_output_with_colon_in_ssid()
    {
        var w = NetshWlanParser.Parse(NetshPt);
        Assert.NotNull(w);
        Assert.Equal("Padaria: Wi-Fi Clientes", w!.Ssid);
        Assert.Equal(40, w.SignalPercent);
        Assert.Equal(-80, w.RssiDbm);
    }

    [Fact]
    public void Prefers_reported_rssi()
    {
        var w = NetshWlanParser.Parse(NetshWin11Rssi);
        Assert.Equal(-41, w!.RssiDbm);
        Assert.Equal("Mercado", w.Ssid);
    }

    [Fact]
    public void Disconnected_or_empty_returns_null()
    {
        Assert.Null(NetshWlanParser.Parse(NetshDisconnected));
        Assert.Null(NetshWlanParser.Parse(""));
        Assert.Null(NetshWlanParser.Parse("The Wireless AutoConfig Service (wlansvc) is not running."));
    }

    [Theory]
    [InlineData(0, -100)]
    [InlineData(100, -50)]
    [InlineData(86, -57)]
    [InlineData(150, -50)]
    [InlineData(-3, -100)]
    public void Quality_to_dbm(int q, int dbm) => Assert.Equal(dbm, TelemetryMath.QualityToDbm(q));

    [Fact]
    public void Cpu_percent_from_deltas()
    {
        Assert.Equal(25, TelemetryMath.CpuPercent(idleDelta: 750, kernelDelta: 800, userDelta: 200));
        Assert.Equal(0, TelemetryMath.CpuPercent(1000, 1000, 0));
        Assert.Null(TelemetryMath.CpuPercent(0, 0, 0));
        Assert.Null(TelemetryMath.CpuPercent(2000, 1000, 0));
    }

    [Theory]
    [InlineData("Windows 10 Pro", "22H2", "19045", 4291, "Windows 10 Pro 22H2 (build 19045.4291)")]
    [InlineData("Windows 10 Pro", "23H2", "22631", 3447, "Windows 11 Pro 23H2 (build 22631.3447)")]
    [InlineData("Windows 10 IoT Enterprise LTSC 2021", null, "19044", null, "Windows 10 IoT Enterprise LTSC 2021 (build 19044)")]
    [InlineData(null, null, null, null, "Windows")]
    public void Windows_version_format(string? product, string? display, string? build, int? ubr, string expected) =>
        Assert.Equal(expected, TelemetryMath.FormatWindowsVersion(product, display, build, ubr));

    [Fact]
    public void Orientation_and_resolution()
    {
        Assert.Equal("LANDSCAPE", TelemetryMath.Orientation(1920, 1080));
        Assert.Equal("PORTRAIT", TelemetryMath.Orientation(1080, 1920));
        Assert.Equal("1920x1080", TelemetryMath.Resolution(1920, 1080));
    }

    [Fact]
    public void Snapshot_json_has_rdpp_fields_and_no_ip()
    {
        var s = new TelemetrySnapshot(12, 1500, 4000, 20000, 60000, 3600, "Windows 10 Pro", "1.0.0", "1920x1080", "LANDSCAPE",
            new WifiInfo("Loja", 80, -60));
        var json = s.ToJson().ToJsonString();
        foreach (var k in new[] { "cpu_pct", "ram_used_mb", "ram_total_mb", "disk_free_mb", "disk_total_mb", "uptime_s", "os", "app_version", "platform", "resolution", "orientation", "wifi", "hdmi_connected", "ssid", "rssi_dbm" })
            Assert.Contains("\"" + k + "\"", json);
        Assert.DoesNotContain("ip", json.ToLowerInvariant().Replace("ssid", ""));
        Assert.Contains("\"platform\":\"windows\"", json);
    }
}
