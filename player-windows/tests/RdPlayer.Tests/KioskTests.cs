using System.Text;
using RdPlayer.Core.Input;
using RdPlayer.Core.Platform;
using RdPlayer.Core.Security;

namespace RdPlayer.Tests;

public class KioskTests
{
    private static readonly ShortcutPolicy Policy = new(KeyChord.TryParse("Ctrl+Alt+Shift+F12", out var k) ? k : default);

    private static KeyChord C(string s) => KeyChord.TryParse(s, out var k) ? k : throw new ArgumentException(s);

    [Theory]
    [InlineData("Alt+F4")]
    [InlineData("Ctrl+W")]
    [InlineData("Ctrl+F4")]
    [InlineData("F5")]
    [InlineData("Ctrl+F5")]
    [InlineData("Shift+F5")]
    [InlineData("Ctrl+R")]
    [InlineData("Ctrl+Shift+R")]
    [InlineData("F11")]
    [InlineData("F12")]
    [InlineData("Ctrl+Shift+I")]
    [InlineData("Ctrl+Shift+J")]
    [InlineData("Ctrl+Shift+C")]
    [InlineData("Ctrl+U")]
    [InlineData("Ctrl+P")]
    [InlineData("Ctrl+S")]
    [InlineData("Ctrl+O")]
    [InlineData("Ctrl+N")]
    [InlineData("Ctrl+T")]
    [InlineData("Ctrl+F")]
    [InlineData("Ctrl+L")]
    [InlineData("Ctrl+0")]
    [InlineData("Ctrl+Tab")]
    [InlineData("Alt+Left")]
    [InlineData("Alt+Right")]
    [InlineData("Alt+Home")]
    [InlineData("Alt+Space")]
    [InlineData("Ctrl+Alt+Delete")]
    [InlineData("Win+D")]
    public void Casual_close_reload_devtools_shortcuts_blocked(string chord)
    {
        Assert.Equal(KeyDecision.Block, Policy.Classify(C(chord)));
    }

    [Fact]
    public void Modifier_keys_alone_allowed()
    {
        Assert.Equal(KeyDecision.Allow, Policy.Classify(new KeyChord(Vk.Control, true, false, false)));
        Assert.Equal(KeyDecision.Allow, Policy.Classify(new KeyChord(Vk.Shift, false, false, true)));
        Assert.Equal(KeyDecision.Allow, Policy.Classify(new KeyChord(Vk.Menu, false, true, false)));
        Assert.Equal(KeyDecision.Block, Policy.Classify(new KeyChord(Vk.LWin, false, false, false, Win: true)));
    }

    [Fact]
    public void Browser_keys_blocked()
    {
        Assert.Equal(KeyDecision.Block, Policy.Classify(new KeyChord(Vk.BrowserBack, false, false, false)));
        Assert.Equal(KeyDecision.Block, Policy.Classify(new KeyChord(0xA8, false, false, false))); // BROWSER_REFRESH
        Assert.Equal(KeyDecision.Block, Policy.Classify(new KeyChord(Vk.Apps, false, false, false)));
        Assert.Equal(KeyDecision.Block, Policy.Classify(new KeyChord(Vk.OemPlus, true, false, false))); // zoom
    }

    [Theory]
    [InlineData("A")]
    [InlineData("Shift+A")]
    [InlineData("Ctrl+C")]
    [InlineData("Ctrl+V")]
    [InlineData("Ctrl+A")]
    [InlineData("Ctrl+Alt+Q")] // AltGr+Q = "/" no ABNT2
    [InlineData("Esc")]
    [InlineData("Enter")]
    [InlineData("Left")]
    [InlineData("Backspace")]
    public void Typing_allowed(string chord)
    {
        Assert.Equal(KeyDecision.Allow, Policy.Classify(C(chord)));
    }

    [Fact]
    public void Tech_shortcut_opens_menu()
    {
        Assert.Equal(KeyDecision.TechMenu, Policy.Classify(C("Ctrl+Alt+Shift+F12")));
        Assert.Equal(KeyDecision.Block, Policy.Classify(C("Ctrl+Alt+F12")));
    }

    [Fact]
    public void Windowed_mode_still_blocks_reload_and_devtools()
    {
        var p = new ShortcutPolicy(C("Ctrl+Alt+Shift+F12"), kioskEnabled: false);
        Assert.Equal(KeyDecision.Block, p.Classify(C("F5")));
        Assert.Equal(KeyDecision.Block, p.Classify(C("Ctrl+Shift+I")));
        Assert.Equal(KeyDecision.Allow, p.Classify(C("Alt+F4")));
    }

    [Fact]
    public void System_keys_for_low_level_hook()
    {
        Assert.True(ShortcutPolicy.IsBlockedSystemKey(Vk.LWin, false, false));
        Assert.True(ShortcutPolicy.IsBlockedSystemKey(Vk.Tab, false, true));
        Assert.True(ShortcutPolicy.IsBlockedSystemKey(Vk.Escape, true, false));
        Assert.True(ShortcutPolicy.IsBlockedSystemKey(Vk.Escape, false, true));
        Assert.False(ShortcutPolicy.IsBlockedSystemKey(Vk.Tab, false, false));
        Assert.False(ShortcutPolicy.IsBlockedSystemKey('A', true, false));
    }

    [Theory]
    [InlineData("ctrl+shift+f10", "Ctrl+Shift+F10")]
    [InlineData("Control + Alt + M", "Ctrl+Alt+M")]
    [InlineData("F24", "F24")]
    public void Chord_parse_and_format(string input, string expected)
    {
        Assert.True(KeyChord.TryParse(input, out var k));
        Assert.Equal(expected, k.ToString());
    }

    [Theory]
    [InlineData("")]
    [InlineData("Ctrl+")]
    [InlineData("Ctrl+A+B")]
    [InlineData("Ctrl+F25")]
    [InlineData("Hyper+X")]
    public void Chord_parse_rejects(string input) => Assert.False(KeyChord.TryParse(input, out _));

    [Fact]
    public void Pin_hash_roundtrip_and_wrong_pin()
    {
        var h = PinHash.Create("482913", iterations: 2000);
        Assert.StartsWith("pbkdf2-sha256$2000$", h);
        Assert.True(PinHash.Verify("482913", h));
        Assert.False(PinHash.Verify("482914", h));
        Assert.False(PinHash.Verify("", h));
        Assert.False(PinHash.Verify("482913", "pbkdf2-sha256$10$AAAA$BBBB"));
        Assert.Throws<ArgumentException>(() => PinHash.Create("12"));
        Assert.Throws<ArgumentException>(() => PinHash.Create("12 34"));
    }

    [Fact]
    public void Pin_hash_matches_standard_pbkdf2_vector()
    {
        // PBKDF2-HMAC-SHA256("password","salt",4096,32) — vetor público (RFC 7914 §11 / conjunto de testes amplamente usado)
        var salt = Convert.ToBase64String(Encoding.UTF8.GetBytes("salt"));
        var dk = Convert.ToBase64String(Convert.FromHexString("c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a"));
        Assert.False(PinHash.Verify("password", $"pbkdf2-sha256$4096${salt}${dk}")); // sal de 4 bytes < 8 → formato recusado
        var salt8 = Encoding.UTF8.GetBytes("saltsalt");
        var mine = PinHash.Create("password", 4096, salt8);
        Assert.True(PinHash.Verify("password", mine));
        // mesma derivação calculada de forma independente
        var expected = System.Security.Cryptography.Rfc2898DeriveBytes.Pbkdf2("password"u8.ToArray(), salt8, 4096, System.Security.Cryptography.HashAlgorithmName.SHA256, 32);
        Assert.EndsWith("$" + Convert.ToBase64String(expected), mine);
        Assert.Equal("c5e478d59288c841aa530db6845c4c8d962893a001ce4e11a4963873aa98134a",
            Convert.ToHexString(System.Security.Cryptography.Rfc2898DeriveBytes.Pbkdf2("password"u8.ToArray(), "salt"u8.ToArray(), 4096, System.Security.Cryptography.HashAlgorithmName.SHA256, 32)).ToLowerInvariant());
    }

    [Fact]
    public void Pin_hash_generated_by_install_ps1_on_powershell_51_verifies()
    {
        // gerado no host (Windows PowerShell 5.1.19041 / .NET Framework 4.8) com o mesmo código do install.ps1
        // (Rfc2898DeriveBytes(pin, sal=01..10, 1000, SHA256).GetBytes(32)) — prova de compatibilidade instalador ↔ casca
        const string fromPs = "pbkdf2-sha256$1000$AQIDBAUGBwgJCgsMDQ4PEA==$p59oLf9zAFjwMMGuXw2WSkkvx4MOvRYygCn8xWlqdMs=";
        Assert.True(PinHash.Verify("482913", fromPs));
        Assert.False(PinHash.Verify("482912", fromPs));
        Assert.Equal(fromPs, PinHash.Create("482913", 1000, Enumerable.Range(1, 16).Select(i => (byte)i).ToArray()));
    }

    [Fact]
    public void Pin_guard_locks_after_max_attempts()
    {
        var g = new PinGuard(maxAttempts: 3, lockoutMs: 300_000);
        Assert.True(g.Register(0, false));
        Assert.True(g.Register(1, false));
        Assert.False(g.IsLocked(2, out _));
        Assert.True(g.Register(2, false));
        Assert.True(g.IsLocked(3, out var rem));
        Assert.True(rem > 299_000);
        Assert.False(g.Register(10, true)); // ignorado enquanto bloqueado
        Assert.False(g.IsLocked(300_003, out _));
        Assert.True(g.Register(300_004, true));
        Assert.Equal(0, g.Failures);
    }

    [Fact]
    public void Reboot_policy()
    {
        Assert.False(RebootPolicy.Decide(1_000_000, null, allowed: true, 60, smokeTest: true).Allowed);
        Assert.Equal("REMOTE_REBOOT_DISABLED", RebootPolicy.Decide(1_000_000, null, allowed: false, 60, false).Reason);
        Assert.True(RebootPolicy.Decide(10_000_000, null, true, 60, false).Allowed);
        Assert.Equal("REBOOT_RATE_LIMITED", RebootPolicy.Decide(10_000_000, 10_000_000 - 59 * 60_000, true, 60, false).Reason);
        Assert.True(RebootPolicy.Decide(10_000_000, 10_000_000 - 61 * 60_000, true, 60, false).Allowed);
        Assert.True(RebootPolicy.Decide(10_000_000, 20_000_000, true, 60, false).Allowed); // relógio voltou: não trava para sempre
        Assert.Contains("/r /t 30", RebootPolicy.ShutdownArguments(30));
        Assert.Contains("/t 5 ", RebootPolicy.ShutdownArguments(1));
    }

    [Fact]
    public void Autostart_task_xml_is_logon_trigger_with_restart_and_escaped()
    {
        var xml = Autostart.BuildTaskXml(@"C:\Users\Zé & Cia\AppData\Local\RdMidiaIndoor\Player\RdPlayer.Watchdog.exe", @"PC\Zé");
        var doc = System.Xml.Linq.XDocument.Parse(xml);
        System.Xml.Linq.XNamespace ns = "http://schemas.microsoft.com/windows/2004/02/mit/task";
        Assert.NotNull(doc.Root!.Element(ns + "Triggers")!.Element(ns + "LogonTrigger"));
        Assert.Equal(@"PC\Zé", doc.Root.Element(ns + "Triggers")!.Element(ns + "LogonTrigger")!.Element(ns + "UserId")!.Value);
        var settings = doc.Root.Element(ns + "Settings")!;
        Assert.Equal("PT1M", settings.Element(ns + "RestartOnFailure")!.Element(ns + "Interval")!.Value);
        Assert.Equal("false", settings.Element(ns + "DisallowStartIfOnBatteries")!.Value);
        Assert.Equal("PT0S", settings.Element(ns + "ExecutionTimeLimit")!.Value);
        Assert.Equal("LeastPrivilege", doc.Root.Element(ns + "Principals")!.Element(ns + "Principal")!.Element(ns + "RunLevel")!.Value);
        var exec = doc.Root.Element(ns + "Actions")!.Element(ns + "Exec")!;
        Assert.EndsWith(@"Zé & Cia\AppData\Local\RdMidiaIndoor\Player\RdPlayer.Watchdog.exe", exec.Element(ns + "Command")!.Value);
        Assert.Equal("--autostart", exec.Element(ns + "Arguments")!.Value);
        Assert.Equal("\"C:\\x\\RdPlayer.Watchdog.exe\" --autostart", Autostart.RunValue(@"C:\x\RdPlayer.Watchdog.exe"));
    }
}
