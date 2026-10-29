using System.IO.Compression;
using System.Text;
using RdPlayer.Core.Update;

namespace RdPlayer.Tests;

public sealed class SlotsAndZipTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), "rdslots-" + Guid.NewGuid().ToString("N"));

    public SlotsAndZipTests() => Directory.CreateDirectory(_root);

    public void Dispose()
    {
        try { Directory.Delete(_root, true); } catch { }
    }

    private static void MakeVersionDir(string dir, string version, bool withExe = true)
    {
        Directory.CreateDirectory(dir);
        if (withExe) File.WriteAllText(Path.Combine(dir, UpdateSlots.ShellExeName), "exe " + version);
        File.WriteAllText(Path.Combine(dir, UpdateSlots.VersionFileName), "{\"version\":\"" + version + "\"}");
    }

    [Fact]
    public void Promote_then_rollback_restores_previous()
    {
        var slots = new UpdateSlots(_root);
        MakeVersionDir(slots.CurrentDir, "1.0.0");
        var staged = slots.StagingDirFor("1.1.0");
        MakeVersionDir(staged, "1.1.0");

        slots.Promote(staged);
        Assert.Equal("1.1.0", UpdateSlots.ReadVersion(slots.CurrentDir));
        Assert.Equal("1.0.0", UpdateSlots.ReadVersion(slots.PreviousDir));
        Assert.False(Directory.Exists(staged));

        Assert.True(slots.Rollback());
        Assert.Equal("1.0.0", UpdateSlots.ReadVersion(slots.CurrentDir));
        Assert.False(Directory.Exists(slots.PreviousDir));
        Assert.False(Directory.Exists(slots.FailedDir));
        Assert.False(slots.Rollback());
    }

    [Fact]
    public void Second_promote_discards_oldest()
    {
        var slots = new UpdateSlots(_root);
        MakeVersionDir(slots.CurrentDir, "1.0.0");
        var s1 = slots.StagingDirFor("1.1.0"); MakeVersionDir(s1, "1.1.0"); slots.Promote(s1);
        var s2 = slots.StagingDirFor("1.2.0"); MakeVersionDir(s2, "1.2.0"); slots.Promote(s2);
        Assert.Equal("1.2.0", UpdateSlots.ReadVersion(slots.CurrentDir));
        Assert.Equal("1.1.0", UpdateSlots.ReadVersion(slots.PreviousDir));
    }

    [Fact]
    public void Staging_dir_rejects_path_tricks()
    {
        var slots = new UpdateSlots(_root);
        Assert.Throws<ArgumentException>(() => slots.StagingDirFor("../../x"));
        Assert.Throws<ArgumentException>(() => slots.StagingDirFor("1.0"));
    }

    [Fact]
    public void Package_validation()
    {
        var d = Path.Combine(_root, "pkg");
        MakeVersionDir(d, "2.0.0");
        Assert.Null(UpdateSlots.ValidatePackageDir(d, "2.0.0"));
        Assert.NotNull(UpdateSlots.ValidatePackageDir(d, "2.0.1"));
        var d2 = Path.Combine(_root, "pkg2");
        MakeVersionDir(d2, "2.0.0", withExe: false);
        Assert.NotNull(UpdateSlots.ValidatePackageDir(d2, "2.0.0"));
    }

    private string MakeZip(params (string Name, string Content)[] entries)
    {
        var path = Path.Combine(_root, Guid.NewGuid().ToString("N") + ".zip");
        using var fs = File.Create(path);
        using var za = new ZipArchive(fs, ZipArchiveMode.Create);
        foreach (var (name, content) in entries)
        {
            var e = za.CreateEntry(name);
            using var s = e.Open();
            var b = Encoding.UTF8.GetBytes(content);
            s.Write(b, 0, b.Length);
        }
        return path;
    }

    [Fact]
    public void SafeZip_extracts_normal_package()
    {
        var zip = MakeZip(("RdPlayer.Shell.exe", "x"), ("wwwroot/index.html", "<html>"), ("version.json", "{\"version\":\"1.0.0\"}"));
        var dest = Path.Combine(_root, "out");
        SafeZip.ExtractToDirectory(zip, dest);
        Assert.True(File.Exists(Path.Combine(dest, "wwwroot", "index.html")));
        Assert.Null(UpdateSlots.ValidatePackageDir(dest, "1.0.0"));
    }

    [Theory]
    [InlineData("../evil.txt")]
    [InlineData("a/../../evil.txt")]
    [InlineData("/abs.txt")]
    [InlineData("C:/abs.txt")]
    [InlineData("..\\evil.txt")]
    public void SafeZip_rejects_zip_slip(string name)
    {
        var zip = MakeZip((name, "x"));
        var dest = Path.Combine(_root, "out-" + Guid.NewGuid().ToString("N"));
        Assert.Throws<InvalidDataException>(() => SafeZip.ExtractToDirectory(zip, dest));
        Assert.False(File.Exists(Path.Combine(_root, "evil.txt")));
    }

    [Fact]
    public void SafeZip_enforces_total_size_limit()
    {
        var zip = MakeZip(("a.bin", new string('x', 5000)), ("b.bin", new string('y', 5000)));
        Assert.Throws<InvalidDataException>(() => SafeZip.ExtractToDirectory(zip, Path.Combine(_root, "lim"), maxTotalBytes: 6000));
    }

    [Fact]
    public void Update_state_roundtrip()
    {
        var p = Path.Combine(_root, "state", "update_state.json");
        var s = StateStore.LoadUpdateState(p);
        Assert.Null(s.Pending);
        s.Pending = new PendingUpdate { Version = "1.1.0", StagedDir = "x", Sha256 = new string('a', 64), CreatedMs = 5 };
        s.BadVersions.Add("1.0.9");
        StateStore.SaveUpdateState(p, s);
        var s2 = StateStore.LoadUpdateState(p);
        Assert.Equal("1.1.0", s2.Pending!.Version);
        Assert.Contains("1.0.9", s2.BadVersions);
        Assert.Contains("\"bad_versions\"", File.ReadAllText(p));
        File.WriteAllText(p, "{corrompido");
        Assert.Null(StateStore.LoadUpdateState(p).Pending);
    }
}
