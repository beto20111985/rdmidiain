namespace RdPlayer.Core.Platform;

public readonly record struct PolicyDecision(bool Allowed, string Reason);

/// <summary>
/// Política do comando remoto "reboot": precisa estar habilitado no config.json e respeita intervalo mínimo
/// entre reinícios remotos (evita laço de reboot). O atraso dá tempo para o command_ack sair e pode ser
/// cancelado localmente com <c>shutdown /a</c>.
/// </summary>
public static class RebootPolicy
{
    public static PolicyDecision Decide(long nowMs, long? lastRemoteRebootMs, bool allowed, int minIntervalMinutes, bool smokeTest)
    {
        if (smokeTest) return new(false, "SMOKE_TEST");
        if (!allowed) return new(false, "REMOTE_REBOOT_DISABLED");
        if (lastRemoteRebootMs is long last)
        {
            long minMs = (long)minIntervalMinutes * 60_000;
            if (nowMs >= last && nowMs - last < minMs) return new(false, "REBOOT_RATE_LIMITED");
        }
        return new(true, "OK");
    }

    public static string ShutdownArguments(int delaySeconds) =>
        $"/r /t {Math.Clamp(delaySeconds, 5, 600)} /d p:4:1 /c \"RD Midia Indoor: reinicio remoto solicitado pelo painel\"";
}
