package br.com.rdmidiaindoor.player.core

/**
 * O que o runtime deve exibir a partir de `startMs` (tempo corrigido) e até quando.
 *
 * - [segment]: segmento resolvido que será exibido (para entrada no meio de item, um FILLER sintético).
 * - [targetEndMs]: instante-alvo da próxima troca (vídeo nunca é cortado: o runtime ainda espera o fim do decodificador).
 * - [recordPop]: gera evento PoP (somente ITEM; FILLER nunca gera PoP — §8.2.9).
 * - [skipped]: itens perdidos (entrada no meio, preempção por emergência) ⇒ eventos SKIPPED.
 */
data class Decision(
    val segment: Segment,
    val startMs: Long,
    val targetEndMs: Long,
    val expectedMs: Long,
    val recordPop: Boolean,
    val skipped: List<Segment>,
    val lateMs: Long,
)

/**
 * Regras de RUNTIME sobre a função pura [CycleResolver.resolve] (RDPP §5.1 itens 6–7, §5.2, §8.2.9–10).
 *
 * Pontualidade × 100 %: cada ITEM precisa ficar visível ≥ d (sem tolerância) e os intervalos de PoP são
 * contíguos e sem sobreposição. Atrasos de troca (milissegundos) acumulariam; por isso:
 * - itens de kinds protegidos (EXTERNAL/INSTITUTIONAL) e de emergência sempre recebem d completo
 *   (fim-alvo = max(fim do segmento, início real + d));
 * - itens INTERNAL/FALLBACK absorvem o atraso acumulado quando ele passa de [absorbThresholdMs]
 *   (fim-alvo = fim do segmento; o PoP registra o tempo real e, se < d, o resultado não é COMPLETED);
 * - FILLER sempre termina na fronteira absoluta;
 * - atraso > [joinToleranceMs] ⇒ tratado como entrada no meio do item (FILLER até o fim + SKIPPED).
 */
class PlaybackPlanner(
    resolver: CycleResolver,
    private val kindOf: ((String) -> String?)? = null,
    val joinToleranceMs: Long = 1_500L,
    val absorbThresholdMs: Long = 250L,
) {
    @Volatile
    var resolver: CycleResolver = resolver
        private set

    private val reported = HashSet<String>()
    private val reportedOrder = ArrayDeque<String>()

    fun replaceResolver(r: CycleResolver) {
        resolver = r
    }

    private fun key(s: Segment) = "${s.startMs}:${s.creativeKey}:${s.block}"

    private fun markReported(s: Segment): Boolean {
        val k = key(s)
        if (!reported.add(k)) return false
        reportedOrder.addLast(k)
        while (reportedOrder.size > 1024) reported.remove(reportedOrder.removeFirst())
        return true
    }

    /** O runtime marca o item que começou a tocar (erro/kill no meio não geram SKIPPED duplicado). */
    fun markStarted(s: Segment) {
        markReported(s)
    }

    fun isProtectedKind(kind: String?): Boolean = kind == "EXTERNAL" || kind == "INSTITUTIONAL"

    /** Decide o que exibir agora (t = tempo corrigido). Muta apenas o registro de SKIPPED já emitidos. */
    fun decide(t: Long): Decision {
        val r = resolver
        val seg = r.resolve(t)
        val skipped = ArrayList<Segment>()

        if (seg.block == Block.EMERGENCY) {
            // itens normais (não-fallback) preemptados ⇒ SKIPPED (uma vez cada)
            for (n in r.normalSegmentsBetween(t, seg.endMs)) {
                if (n.kind == SegmentKind.ITEM && !n.fromFallback && n.creativeKey != null && markReported(n)) skipped += n
            }
        }

        val late = t - seg.startMs
        if (seg.kind == SegmentKind.FILLER) {
            return Decision(seg.copy(startMs = t), t, seg.endMs, seg.durationMs, false, skipped, late)
        }

        if (late > joinToleranceMs) {
            // Entrada no meio do ITEM: FILLER até o fim do item + SKIPPED do item (§5.2, literal: qualquer ITEM,
            // inclusive de fallback). Só a preempção por emergência exclui itens de fallback (§5.2).
            if (seg.creativeKey != null && markReported(seg)) skipped += seg
            val fillerKey = if (seg.block == Block.EMERGENCY) seg.creativeKey else r.fillerCreativeAt(t)
            val filler = Segment(seg.block, SegmentKind.FILLER, fillerKey, t, seg.endMs, fromFallback = true)
            return Decision(filler, t, seg.endMs, seg.durationMs, false, skipped, late)
        }

        val d = seg.durationMs
        val kind = seg.creativeKey?.let { k -> kindOf?.invoke(k) ?: r.manifest.creatives[k]?.kind }
        val fullDuration = maxOf(seg.endMs, t + d)
        val target = when {
            seg.block == Block.EMERGENCY -> fullDuration
            isProtectedKind(kind) -> fullDuration
            late >= absorbThresholdMs -> seg.endMs // absorve o atraso acumulado (conteúdo não comissionado)
            else -> fullDuration
        }
        return Decision(seg, t, target, d, seg.creativeKey != null, skipped, late)
    }

    /** Previsão (sem efeitos) do segmento a exibir em `t` — usada para pré-carregar o próximo item. */
    fun peek(t: Long): Segment {
        val seg = resolver.resolve(t)
        if (seg.kind == SegmentKind.ITEM && t - seg.startMs > joinToleranceMs) {
            val fillerKey = if (seg.block == Block.EMERGENCY) seg.creativeKey else resolver.fillerCreativeAt(t)
            return Segment(seg.block, SegmentKind.FILLER, fillerKey, t, seg.endMs, fromFallback = true)
        }
        return seg
    }
}
