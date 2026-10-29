package br.com.rdmidiaindoor.player.core

/**
 * Relógio corrigido do player: `t = relógio local + clock_offset_ms` (RDPP §5.1).
 * O offset é medido a partir do header `X-Server-Time-Ms` (e da mensagem WS `server_time`), com
 * compensação de meia ida-e-volta. [monotonicMs] (SystemClock.elapsedRealtime no Android) mede durações de PoP.
 */
class ClockService(
    private val wallMs: () -> Long,
    val monotonicMs: () -> Long,
    initialOffsetMs: Long = 0L,
    private val persist: (Long) -> Unit = {},
) {
    @Volatile
    var offsetMs: Long = initialOffsetMs
        private set

    /** true depois de pelo menos uma medição com o servidor desde que o processo iniciou. */
    @Volatile
    var measuredThisBoot: Boolean = false
        private set

    @Volatile
    var lastMeasurementMonoMs: Long = Long.MIN_VALUE
        private set

    /** Último contato bem-sucedido com o servidor (qualquer resposta com hora do servidor), relógio monotônico. */
    @Volatile
    var lastContactMonoMs: Long = Long.MIN_VALUE
        private set

    @Volatile
    private var bestRttMs: Long = Long.MAX_VALUE

    fun now(): Long = wallMs() + offsetMs

    fun localNow(): Long = wallMs()

    /** [sentLocalMs]/[receivedLocalMs] = relógio local no envio e no recebimento da resposta. */
    fun onServerTime(serverMs: Long, sentLocalMs: Long, receivedLocalMs: Long) {
        lastContactMonoMs = monotonicMs()
        val rtt = (receivedLocalMs - sentLocalMs).coerceAtLeast(0L)
        val candidate = serverMs - (sentLocalMs + rtt / 2)
        val jump = if (measuredThisBoot) kotlin.math.abs(candidate - offsetMs) else Long.MAX_VALUE
        if (measuredThisBoot) {
            // Achado do E2E real: cada amostra ruidosa movia o relógio para trás/frente e a cadeia de PoP sofria.
            // Mudanças pequenas (jitter de rede) são ignoradas; amostra de RTT ruim só vale para saltos grandes
            // (relógio do aparelho realmente ajustado).
            if (jump < JITTER_DEADBAND_MS) {
                if (rtt < bestRttMs) bestRttMs = rtt
                return
            }
            if (rtt > maxOf(2 * bestRttMs, 500L) && jump < LARGE_JUMP_MS) return
        }
        offsetMs = candidate
        measuredThisBoot = true
        lastMeasurementMonoMs = monotonicMs()
        if (rtt < bestRttMs || jump >= LARGE_JUMP_MS) bestRttMs = rtt
        persist(offsetMs)
    }

    /** Online para o PoP (`offline` = false): contato com o servidor nos últimos [windowMs] ou WS conectado. */
    fun contactedWithin(windowMs: Long): Boolean =
        lastContactMonoMs != Long.MIN_VALUE && monotonicMs() - lastContactMonoMs <= windowMs

    /**
     * Alerta "Corrija a data e hora" (§5.1 item 10): data local anterior ao `generated_at_ms` do manifesto ativo,
     * ou offset > 120 s sem medição nesta sessão (sem rede para corrigir).
     */
    fun clockAlert(activeGeneratedAtMs: Long?): Boolean {
        if (activeGeneratedAtMs != null && now() < activeGeneratedAtMs) return true
        return kotlin.math.abs(offsetMs) > MAX_OFFSET_MS && !measuredThisBoot
    }

    companion object {
        const val MAX_OFFSET_MS = 120_000L
        const val JITTER_DEADBAND_MS = 150L
        const val LARGE_JUMP_MS = 2_000L
    }
}

/** Alertas de falta de sincronização (RDPP §5.1 item 10; ETAPA2 #262–#265). */
object OfflineAlerts {
    enum class Level { NONE, FOOTER, CENTER }

    private const val DAY_MS = 86_400_000L

    fun level(nowMs: Long, lastSyncMs: Long?, offlineAlertDays: List<Int>): Level {
        if (lastSyncMs == null) return Level.NONE
        val days = offlineAlertDays.ifEmpty { listOf(7, 28) }
        val elapsed = nowMs - lastSyncMs
        val centerDays = days.getOrNull(1)
        return when {
            centerDays != null && elapsed >= centerDays * DAY_MS -> Level.CENTER
            elapsed >= days[0] * DAY_MS -> Level.FOOTER
            else -> Level.NONE
        }
    }

    fun daysWithoutSync(nowMs: Long, lastSyncMs: Long?): Long =
        if (lastSyncMs == null) 0 else ((nowMs - lastSyncMs).coerceAtLeast(0L)) / DAY_MS
}
