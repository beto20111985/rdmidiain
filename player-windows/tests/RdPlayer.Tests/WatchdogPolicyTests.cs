using RdPlayer.Core.Ipc;
using RdPlayer.Core.Watchdog;

namespace RdPlayer.Tests;

public class WatchdogPolicyTests
{
    private static readonly WatchdogOptions Opt = new(
        HangTimeoutMs: 20_000, StartupTimeoutMs: 60_000, ProgressTimeoutMs: 30_000,
        StartupGraceMs: 90_000, ReloadGraceMs: 60_000, MaxReloadsBeforeRestart: 3, ReloadWindowMs: 600_000);

    private static HeartbeatSample Hb(long? progressAge, string? state = "PLAYING", long uiAge = 100, bool ready = true) =>
        new(uiAge, progressAge, state, ready);

    [Fact]
    public void Healthy_process_needs_nothing()
    {
        var p = new WatchdogPolicy(Opt);
        p.Reset(0);
        for (long t = 5_000; t <= 600_000; t += 5_000)
        {
            p.OnHeartbeat(t, Hb(progressAge: 2_000));
            Assert.Equal(WatchdogActionKind.None, p.Evaluate(t).Kind);
        }
    }

    [Fact]
    public void No_heartbeat_at_startup_restarts_after_timeout()
    {
        var p = new WatchdogPolicy(Opt);
        p.Reset(0);
        Assert.Equal(WatchdogActionKind.None, p.Evaluate(59_000).Kind);
        var a = p.Evaluate(61_000);
        Assert.Equal(WatchdogActionKind.Restart, a.Kind);
        Assert.Equal("NO_HEARTBEAT_AT_STARTUP", a.Reason);
    }

    [Fact]
    public void Heartbeat_stops_means_hang()
    {
        var p = new WatchdogPolicy(Opt);
        p.Reset(0);
        p.OnHeartbeat(5_000, Hb(1_000));
        Assert.Equal(WatchdogActionKind.None, p.Evaluate(24_000).Kind);
        Assert.Equal("HEARTBEAT_TIMEOUT", p.Evaluate(25_001).Reason);
    }

    [Fact]
    public void Ui_thread_hung_detected_even_with_heartbeats()
    {
        var p = new WatchdogPolicy(Opt);
        p.Reset(0);
        p.OnHeartbeat(30_000, Hb(1_000, uiAge: 25_000));
        Assert.Equal("UI_THREAD_HUNG", p.Evaluate(30_000).Reason);
    }

    [Fact]
    public void Progress_stall_reloads_after_grace_then_restarts_after_three_reloads()
    {
        var p = new WatchdogPolicy(Opt);
        p.Reset(0);
        long t = 0;
        // bundle reportou progresso até t=10 s e depois parou; heartbeats continuam
        int reloads = 0;
        WatchdogAction last = WatchdogAction.None;
        while (t < 900_000)
        {
            t += 5_000;
            long progressAge = t - 10_000;
            p.OnHeartbeat(t, Hb(progressAge));
            last = p.Evaluate(t);
            if (last.Kind == WatchdogActionKind.Reload)
            {
                Assert.True(t >= 90_000, "não recarrega durante a carência de partida");
                reloads++;
                p.OnReloadIssued(t);
            }
            else if (last.Kind == WatchdogActionKind.Restart) break;
        }
        Assert.Equal(3, reloads);
        Assert.Equal(WatchdogActionKind.Restart, last.Kind);
        Assert.Equal("PROGRESS_STALLED_AFTER_RELOADS", last.Reason);
    }

    [Fact]
    public void Reload_grace_respected_and_recovery_stops_reloads()
    {
        var p = new WatchdogPolicy(Opt);
        p.Reset(0);
        p.OnHeartbeat(95_000, Hb(progressAge: 60_000));
        Assert.Equal(WatchdogActionKind.Reload, p.Evaluate(95_000).Kind);
        p.OnReloadIssued(95_000);
        p.OnHeartbeat(120_000, Hb(progressAge: null, state: null));
        Assert.Equal(WatchdogActionKind.None, p.Evaluate(120_000).Kind); // dentro da carência de recarga
        p.OnHeartbeat(150_000, Hb(progressAge: 1_000));
        Assert.Equal(WatchdogActionKind.None, p.Evaluate(160_000).Kind); // voltou a progredir
    }

    [Fact]
    public void Frozen_state_triggers_reload()
    {
        var p = new WatchdogPolicy(Opt);
        p.Reset(0);
        p.OnHeartbeat(100_000, Hb(progressAge: 1_000, state: "FROZEN"));
        var a = p.Evaluate(100_000);
        Assert.Equal(WatchdogActionKind.Reload, a.Kind);
        Assert.Equal("BUNDLE_FROZEN", a.Reason);
    }

    [Fact]
    public void Old_reloads_expire_from_window()
    {
        var p = new WatchdogPolicy(Opt);
        p.Reset(0);
        foreach (var t in new long[] { 100_000, 200_000 })
        {
            p.OnHeartbeat(t, Hb(progressAge: 40_000));
            Assert.Equal(WatchdogActionKind.Reload, p.Evaluate(t).Kind);
            p.OnReloadIssued(t);
        }
        // muito depois (janela de 10 min expirou), nova falha → recarga, não reinício
        p.OnHeartbeat(900_000, Hb(progressAge: 40_000));
        Assert.Equal(WatchdogActionKind.Reload, p.Evaluate(900_000).Kind);
    }

    [Fact]
    public void Monitor_progress_disabled_never_reloads()
    {
        var p = new WatchdogPolicy(Opt with { MonitorProgress = false });
        p.Reset(0);
        p.OnHeartbeat(500_000, Hb(progressAge: 400_000, state: "FROZEN"));
        Assert.Equal(WatchdogActionKind.None, p.Evaluate(500_000).Kind);
    }

    [Fact]
    public void Restart_backoff_grows_and_detects_crash_loop()
    {
        var b = new RestartBackoff();
        var delays = new List<long>();
        long t = 0;
        for (int i = 0; i < 6; i++)
        {
            t += 10_000;
            delays.Add(b.OnUnexpectedExit(t, runDurationMs: 3_000));
        }
        Assert.Equal(new long[] { 1_000, 2_000, 5_000, 10_000, 30_000, 300_000 }, delays);
        Assert.True(b.InCrashLoop);
        // processo estável por > 10 min zera a sequência
        var b2 = new RestartBackoff();
        b2.OnUnexpectedExit(0, 1_000);
        b2.OnUnexpectedExit(10_000, 1_000);
        Assert.Equal(1_000, b2.OnUnexpectedExit(2_000_000, 1_200_000));
    }

    [Fact]
    public void Ipc_messages_roundtrip()
    {
        Assert.True(WdProtocol.TryParse(WdProtocol.Heartbeat(42, 120, null, "PLAYING", true), out var m));
        Assert.Equal("hb", m!.Type);
        Assert.Equal(42, m.GetLong("pid"));
        Assert.Null(m.GetLong("progress_age_ms"));
        Assert.Equal("PLAYING", m.GetString("state"));
        Assert.True(m.GetBool("ready"));
        Assert.True(WdProtocol.TryParse(WdProtocol.ApplyUpdate("1.2.0", @"C:\x\staging\1.2.0"), out var u));
        Assert.Equal(@"C:\x\staging\1.2.0", u!.GetString("staged_dir"));
        Assert.False(WdProtocol.TryParse("{\"x\":1}", out _));
        Assert.False(WdProtocol.TryParse("não json", out _));
        Assert.False(WdProtocol.TryParse(new string('a', WdProtocol.MaxLineChars + 1), out _));
        Assert.Equal("RdPlayer.Watchdog.S1", WdProtocol.PipeName(1));
    }
}
