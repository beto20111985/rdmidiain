using RdPlayer.Core.Config;

namespace RdPlayer.Core.Watchdog;

public enum WatchdogActionKind
{
    None,
    Reload,
    Restart,
}

public readonly record struct WatchdogAction(WatchdogActionKind Kind, string Reason)
{
    public static readonly WatchdogAction None = new(WatchdogActionKind.None, "");
    public static WatchdogAction Reload(string reason) => new(WatchdogActionKind.Reload, reason);
    public static WatchdogAction Restart(string reason) => new(WatchdogActionKind.Restart, reason);
}

public sealed record WatchdogOptions(
    long HangTimeoutMs = 20_000,
    long StartupTimeoutMs = 60_000,
    long ProgressTimeoutMs = 30_000,
    long StartupGraceMs = 90_000,
    long ReloadGraceMs = 60_000,
    int MaxReloadsBeforeRestart = 3,
    long ReloadWindowMs = 600_000,
    bool MonitorProgress = true)
{
    public static WatchdogOptions FromConfig(WatchdogConfig c) => new(
        c.HangTimeoutMs, c.StartupTimeoutMs, c.ProgressTimeoutMs, c.StartupGraceMs, c.ReloadGraceMs,
        c.MaxReloadsBeforeRestart, c.ReloadWindowMs);
}

/// <summary>Amostra de heartbeat enviada pela casca a cada ~5 s.</summary>
public readonly record struct HeartbeatSample(long UiAgeMs, long? ProgressAgeMs, string? BundleState, bool BundleReady);

/// <summary>
/// Política do watchdog (pura e determinística; relógio injetado):
/// <list type="bullet">
/// <item>sem heartbeat na partida por StartupTimeout, ou sem heartbeat por HangTimeout, ou thread de UI parada → reiniciar processo;</item>
/// <item>bundle sem "progress" por ProgressTimeout (após StartupGrace), ou estado FROZEN → recarregar WebView;</item>
/// <item>MaxReloadsBeforeRestart recargas dentro de ReloadWindow sem recuperação → reiniciar processo.</item>
/// </list>
/// </summary>
public sealed class WatchdogPolicy
{
    private readonly WatchdogOptions _o;
    private readonly Queue<long> _reloads = new();
    private long? _startedAt;
    private long? _lastHeartbeatAt;
    private long _lastUiAge;
    private long? _lastProgressAt;
    private string? _state;
    private long? _lastReloadAt;

    public WatchdogPolicy(WatchdogOptions options) => _o = options;

    public bool BundleReady { get; private set; }
    public long? LastProgressAt => _lastProgressAt;
    public long? LastHeartbeatAt => _lastHeartbeatAt;
    public int RecentReloads => _reloads.Count;

    /// <summary>Novo processo (ou recriação do WebView) iniciado.</summary>
    public void Reset(long nowMs)
    {
        _startedAt = nowMs;
        _lastHeartbeatAt = null;
        _lastUiAge = 0;
        _lastProgressAt = null;
        _state = null;
        _lastReloadAt = null;
        BundleReady = false;
        _reloads.Clear();
    }

    public void Stop() => _startedAt = null;

    public void OnHeartbeat(long nowMs, HeartbeatSample hb)
    {
        _lastHeartbeatAt = nowMs;
        _lastUiAge = Math.Max(0, hb.UiAgeMs);
        _state = hb.BundleState;
        if (hb.BundleReady) BundleReady = true;
        if (hb.ProgressAgeMs is long age && age >= 0)
        {
            var at = nowMs - age;
            if (_lastProgressAt is null || at > _lastProgressAt) _lastProgressAt = at;
        }
    }

    public void OnReloadIssued(long nowMs)
    {
        _lastReloadAt = nowMs;
        _reloads.Enqueue(nowMs);
        _state = null;
    }

    public WatchdogAction Evaluate(long nowMs)
    {
        if (_startedAt is not long started) return WatchdogAction.None;
        long sinceStart = nowMs - started;

        if (_lastHeartbeatAt is not long lastHb)
            return sinceStart > _o.StartupTimeoutMs ? WatchdogAction.Restart("NO_HEARTBEAT_AT_STARTUP") : WatchdogAction.None;
        if (nowMs - lastHb > _o.HangTimeoutMs) return WatchdogAction.Restart("HEARTBEAT_TIMEOUT");
        if (_lastUiAge > _o.HangTimeoutMs) return WatchdogAction.Restart("UI_THREAD_HUNG");

        if (!_o.MonitorProgress || sinceStart < _o.StartupGraceMs) return WatchdogAction.None;
        if (_lastReloadAt is long lr && nowMs - lr < _o.ReloadGraceMs) return WatchdogAction.None;

        bool frozen = string.Equals(_state, "FROZEN", StringComparison.Ordinal);
        long progressRef = _lastProgressAt ?? started;
        bool stalled = nowMs - progressRef > _o.ProgressTimeoutMs;
        if (!stalled && !frozen) return WatchdogAction.None;

        while (_reloads.Count > 0 && nowMs - _reloads.Peek() > _o.ReloadWindowMs) _reloads.Dequeue();
        if (_reloads.Count >= _o.MaxReloadsBeforeRestart)
            return WatchdogAction.Restart(frozen ? "FROZEN_AFTER_RELOADS" : "PROGRESS_STALLED_AFTER_RELOADS");
        return WatchdogAction.Reload(frozen ? "BUNDLE_FROZEN" : "PROGRESS_TIMEOUT");
    }
}

/// <summary>Backoff de reinício após saída inesperada do processo; detecta "crash loop".</summary>
public sealed class RestartBackoff
{
    private static readonly long[] Steps = { 1_000, 2_000, 5_000, 10_000, 30_000, 60_000 };
    private readonly Queue<long> _crashes = new();
    private int _consecutive;

    public long WindowMs { get; init; } = 600_000;
    public long StableAfterMs { get; init; } = 600_000;
    public int CrashLoopThreshold { get; init; } = 6;
    public long CrashLoopDelayMs { get; init; } = 300_000;

    public bool InCrashLoop => _crashes.Count >= CrashLoopThreshold;

    /// <summary>Retorna o atraso (ms) antes de relançar.</summary>
    public long OnUnexpectedExit(long nowMs, long runDurationMs)
    {
        if (runDurationMs >= StableAfterMs) _consecutive = 0;
        _crashes.Enqueue(nowMs);
        while (_crashes.Count > 0 && nowMs - _crashes.Peek() > WindowMs) _crashes.Dequeue();
        long delay = InCrashLoop ? CrashLoopDelayMs : Steps[Math.Min(_consecutive, Steps.Length - 1)];
        _consecutive++;
        return delay;
    }

    public void OnStable() => _consecutive = 0;
}
