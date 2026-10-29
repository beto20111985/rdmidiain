using RdPlayer.Core.Update;

namespace RdPlayer.Tests;

/// <summary>Fluxo do updater: pendente → promovida (probação) → aprovada | revertida com lista negra.</summary>
public sealed class UpdateApplierTests : IDisposable
{
    private readonly string _root = Path.Combine(Path.GetTempPath(), "rdapply-" + Guid.NewGuid().ToString("N"));
    private readonly UpdateSlots _slots;
    private readonly string _state;
    private long _now = 1_000;

    public UpdateApplierTests()
    {
        _slots = new UpdateSlots(_root);
        _state = Path.Combine(_root, "state", "update_state.json");
        MakeVersionDir(_slots.CurrentDir, "1.0.0");
    }

    public void Dispose()
    {
        try { Directory.Delete(_root, true); } catch { }
    }

    private static void MakeVersionDir(string dir, string version, bool valid = true)
    {
        Directory.CreateDirectory(dir);
        File.WriteAllText(Path.Combine(dir, UpdateSlots.ShellExeName), "exe " + version);
        File.WriteAllText(Path.Combine(dir, UpdateSlots.VersionFileName), "{\"version\":\"" + (valid ? version : "9.9.9") + "\"}");
    }

    private UpdateApplier NewApplier() => new(_slots, _state, () => _now);

    private void Stage(string version, bool valid = true, string? stagedDirOverride = null)
    {
        var dir = stagedDirOverride ?? _slots.StagingDirFor(version);
        MakeVersionDir(dir, version, valid);
        var st = StateStore.LoadUpdateState(_state);
        st.Pending = new PendingUpdate { Version = version, StagedDir = dir, Sha256 = new string('a', 64), CreatedMs = _now };
        StateStore.SaveUpdateState(_state, st);
    }

    [Fact]
    public void Apply_then_commit()
    {
        Stage("1.1.0");
        var a = NewApplier();
        var r = a.ApplyPending();
        Assert.True(r.Ok, r.Detail);
        Assert.Equal("1.0.0", r.PreviousVersion);
        Assert.Equal("1.1.0", UpdateSlots.ReadVersion(_slots.CurrentDir));
        var st = a.Load();
        Assert.Null(st.Pending);
        Assert.Equal("1.1.0", st.Probation!.Version);

        _now = 5_000;
        Assert.Equal("1.1.0", a.Commit());
        st = a.Load();
        Assert.Null(st.Probation);
        Assert.Equal("COMMITTED", st.LastResult!.Result);
        Assert.Equal(5_000, st.LastResult.AtMs);
        Assert.False(Directory.Exists(_slots.StagingRoot));
        Assert.Equal("1.0.0", UpdateSlots.ReadVersion(_slots.PreviousDir)); // mantida p/ rollback manual
    }

    [Fact]
    public void Apply_then_crashes_then_rollback_blacklists()
    {
        Stage("1.1.0");
        var a = NewApplier();
        Assert.True(a.ApplyPending().Ok);
        Assert.Equal(1, a.RegisterProbationCrash());
        Assert.Equal(2, a.RegisterProbationCrash());
        var (ok, version) = a.Rollback("caiu 2 vezes");
        Assert.True(ok);
        Assert.Equal("1.1.0", version);
        Assert.Equal("1.0.0", UpdateSlots.ReadVersion(_slots.CurrentDir));
        var st = a.Load();
        Assert.Null(st.Probation);
        Assert.Contains("1.1.0", st.BadVersions);
        Assert.Equal("ROLLED_BACK", st.LastResult!.Result);
        Assert.Equal(0, a.RegisterProbationCrash());
    }

    [Fact]
    public void Invalid_package_is_not_promoted_and_is_blacklisted()
    {
        Stage("1.2.0", valid: false);
        var a = NewApplier();
        var r = a.ApplyPending();
        Assert.False(r.Ok);
        Assert.Equal("1.0.0", UpdateSlots.ReadVersion(_slots.CurrentDir));
        var st = a.Load();
        Assert.Null(st.Pending);
        Assert.Null(st.Probation);
        Assert.Equal("FAILED", st.LastResult!.Result);
        Assert.Contains("1.2.0", st.BadVersions);
        Assert.False(Directory.Exists(_slots.StagingDirFor("1.2.0")));
    }

    [Fact]
    public void Pending_outside_staging_root_is_refused()
    {
        var evil = Path.Combine(_root, "elsewhere", "1.3.0");
        Stage("1.3.0", stagedDirOverride: evil);
        var r = NewApplier().ApplyPending();
        Assert.False(r.Ok);
        Assert.Contains("staging", r.Detail);
        Assert.Equal("1.0.0", UpdateSlots.ReadVersion(_slots.CurrentDir));
    }

    [Fact]
    public void Nothing_pending()
    {
        var r = NewApplier().ApplyPending();
        Assert.False(r.Ok);
        Assert.Equal("NO_PENDING", r.Detail);
    }

    [Fact]
    public void Rollback_without_previous_reports_failure_but_blacklists()
    {
        // instalação nova sem previous: probação falha → não há para onde voltar
        var st = StateStore.LoadUpdateState(_state);
        st.Probation = new ProbationInfo { Version = "1.0.0", StartedMs = 1 };
        StateStore.SaveUpdateState(_state, st);
        var (ok, version) = NewApplier().Rollback("teste");
        Assert.False(ok);
        Assert.Equal("1.0.0", version);
        Assert.Equal("FAILED", NewApplier().Load().LastResult!.Result);
        Assert.Equal("1.0.0", UpdateSlots.ReadVersion(_slots.CurrentDir));
    }
}
