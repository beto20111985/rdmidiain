package br.com.rdmidiaindoor.player.net

import kotlin.random.Random

/**
 * Backoff exponencial com jitter ("equal jitter": metade fixa + metade aleatória) — retry, nunca polling contínuo.
 * Reconexão WS: 1 s → 60 s (RDPP §4). Pareamento sem WS: ≥ 5 s, máx. 60 s (RDPP §2.2).
 */
class Backoff(
    private val baseMs: Long,
    private val maxMs: Long,
    private val factor: Double = 2.0,
    private val random: Random = Random.Default,
) {
    var attempt: Int = 0
        private set

    fun next(): Long {
        val exp = (baseMs * Math.pow(factor, attempt.toDouble())).coerceAtMost(maxMs.toDouble()).toLong()
        attempt++
        val half = exp / 2
        return (half + random.nextLong(exp - half + 1)).coerceIn(minOf(baseMs, maxMs), maxMs)
    }

    fun reset() {
        attempt = 0
    }
}
