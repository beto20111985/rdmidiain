package br.com.rdmidiaindoor.player.core

/**
 * Regras de runtime corrigidas após o E2E real do player web contra o backend (pós-janela, AÇÃO 002).
 * Kotlin puro (testável em JVM).
 */

/**
 * Encadeamento de PoP (RDPP §7, §8.2.10) robusto a correções do relógio.
 *
 * Cada item é ancorado no relógio corrigido ATUAL no instante em que fica visível (`showWallMs`) — nunca herda a base
 * de itens anteriores (uma correção de offset não pode deslocar a cadeia para sempre). Só entre dois registros de PoP
 * consecutivos garante-se a não sobreposição: se o relógio voltou (`d > 0`), primeiro desconta-se do FIM do anterior
 * o que ele durou além do esperado (a espera extra causada pela própria volta do relógio); o resto (raro) desloca o
 * início do novo registro, e a primeira folga seguinte (FILLER/sem PoP) ressincroniza.
 */
object PopChain {
    data class Link(val popStartMs: Long, val trimPreviousMs: Long)

    fun link(prevStartMs: Long, prevMeasuredMs: Long, prevExpectedMs: Long, showWallMs: Long): Link {
        val d = prevStartMs + prevMeasuredMs - showWallMs
        if (d <= 0) return Link(showWallMs, 0)
        val trim = minOf(d, maxOf(0L, prevMeasuredMs - prevExpectedMs))
        return Link(showWallMs + (d - trim), trim)
    }
}

/**
 * Correção do relógio para TRÁS durante um item: o fim agendado (em monotônico) chega antes da fronteira absoluta do
 * segmento. Até [MAX_EXTEND_MS] o item atual continua até a fronteira (sem FILLER relâmpago); acima disso o runtime
 * exibe FILLER até a fronteira.
 */
object PlaybackTiming {
    const val MAX_EXTEND_MS = 5_000L

    /** ms a estender o item atual, ou null se não há extensão a fazer. */
    fun extensionMs(segmentEndMs: Long, nowMs: Long): Long? {
        val remaining = segmentEndMs - nowMs
        return if (remaining in 1..MAX_EXTEND_MS) remaining else null
    }
}

/**
 * Liberação do kill local (RDPP §5.2/§8, achado do E2E real): o kill local só precisa durar até ser ativado um
 * manifesto GERADO DEPOIS dele; a partir daí o `revoked` do manifesto é a fonte de verdade. Sem isso, um kill suspenso
 * ("lift") no painel nunca voltaria a tocar no aparelho. Margem de 2 s contra diferença de relógio.
 */
object KillReconciler {
    const val MARGIN_MS = 2_000L

    /** [kills] = creative_id → instante (ms) em que o kill foi recebido. Devolve os creative_id a liberar. */
    fun lifted(kills: Map<String, Long>, revoked: Set<String>, generatedAtMs: Long): Set<String> =
        kills.filter { (id, at) -> id !in revoked && generatedAtMs > at + MARGIN_MS }.keys
}

/**
 * Instância única de um recurso por processo (motor de reprodução): quem assume despeja o anterior. Evita dois laços
 * de reprodução com a mesma identidade (Activity recriada, `FLAG_ACTIVITY_CLEAR_TASK` do watchdog etc.), que gerariam
 * PoP duplicado/sobreposto.
 */
class SingleOwner<T : Any> {
    private var owner: T? = null

    /** Assume a posse; devolve o dono anterior (já despejado) para ser parado. */
    @Synchronized
    fun claim(candidate: T): T? {
        val previous = owner
        owner = candidate
        return previous?.takeIf { it !== candidate }
    }

    @Synchronized
    fun release(candidate: T) {
        if (owner === candidate) owner = null
    }

    @Synchronized
    fun isOwner(candidate: T): Boolean = owner === candidate
}
