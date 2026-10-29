package br.com.rdmidiaindoor.player.core

import br.com.rdmidiaindoor.player.protocol.Manifest

/** `local_state` do RDPP §5.2: criativos revogados localmente (kill) e chaves de mídia prontas (sha256 verificado). */
data class LocalState(val revoked: Set<String>, val mediaReady: Set<String>)

enum class Block { INTERNAL, EXTERNAL, EMERGENCY }

enum class SegmentKind { ITEM, FILLER }

/**
 * Segmento resolvido (RDPP §5.2). [creativeKey] = chave em `manifest.creatives` (null = tela institucional
 * embutida). [fromFallback] e [insertionId] são metadados de runtime (não fazem parte dos vetores).
 */
data class Segment(
    val block: Block,
    val kind: SegmentKind,
    val creativeKey: String?,
    val startMs: Long,
    val endMs: Long,
    val fromFallback: Boolean = false,
    val insertionId: String? = null,
) {
    val durationMs: Long get() = endMs - startMs
    operator fun contains(t: Long): Boolean = t in startMs until endMs
}

/**
 * Resolução do ciclo ancorado — função pura e determinística (RDPP/1 §5.1 + §5.2 + §8.2).
 * `resolve(t)` devolve o segmento que contém `t`. A regra de "entrada no meio do item" (FILLER + SKIPPED)
 * é do runtime ([PlaybackPlanner]), não desta função.
 */
class CycleResolver(
    val manifest: Manifest,
    val anchorMs: Long,
    val localState: LocalState,
) {
    private val c = manifest.rotation.cycleMs
    private val i = manifest.rotation.internalMs
    private val u = manifest.rotation.unitMs
    private val revoked: Set<String> = manifest.revoked + localState.revoked
    private val cache = object : LinkedHashMap<Long, List<Segment>>(16, 0.75f, true) {
        override fun removeEldestEntry(eldest: MutableMap.MutableEntry<Long, List<Segment>>?) = size > 16
    }

    fun cycleIndex(t: Long): Long = (t - anchorMs).floorDiv(c)
    fun cycleStart(k: Long): Long = anchorMs + k * c
    fun phase(t: Long): Long = t - cycleStart(cycleIndex(t))
    fun isCycleStart(t: Long): Boolean = phase(t) == 0L

    /** Segmento vigente em `t` (emergência preempta tudo). */
    fun resolve(t: Long): Segment = emergencyAt(t) ?: resolveNormal(t)

    /** Segmento da grade normal (ignora emergência) — usado para contabilizar SKIPPED de itens preemptados. */
    fun resolveNormal(t: Long): Segment {
        val k = cycleIndex(t)
        val external = phase(t) >= i
        val segments = blockSegments(k, external)
        return segments.firstOrNull { t in it }
            ?: error("resolução sem segmento para t=$t (k=$k, externo=$external)")
    }

    /** Emergência ativa: primeira entrada com from ≤ t < until; repete em passos de duration_ms; última repetição incompleta = FILLER. */
    fun emergencyAt(t: Long): Segment? {
        val e = manifest.emergency.firstOrNull { it.fromMs <= t && t < it.untilMs } ?: return null
        val creative = manifest.creatives[e.c] ?: return null
        val d = creative.durationMs
        if (d <= 0) return null
        val n = (t - e.fromMs).floorDiv(d)
        val start = e.fromMs + n * d
        val end = start + d
        return if (end > e.untilMs) {
            Segment(Block.EMERGENCY, SegmentKind.FILLER, e.c, start, e.untilMs)
        } else {
            Segment(Block.EMERGENCY, SegmentKind.ITEM, e.c, start, end)
        }
    }

    /** Segmentos normais que intersectam [from, to) — em ordem. */
    fun normalSegmentsBetween(from: Long, to: Long): List<Segment> {
        val out = ArrayList<Segment>()
        var t = from
        var guard = 0
        while (t < to && guard++ < 10_000) {
            val s = resolveNormal(t)
            out += s
            t = s.endMs
        }
        return out
    }

    /** Motivo de inelegibilidade de um criativo de entrada interna/externa no instante `start` (null = elegível). */
    fun ineligibility(creativeKey: String, start: Long): String? {
        val cr = manifest.creatives[creativeKey] ?: return "UNKNOWN_CREATIVE"
        if (cr.creativeId in revoked) return "REVOKED"
        if (start < cr.validFromMs) return "NOT_YET_VALID"
        if (start >= cr.validUntilMs) return "EXPIRED"
        if (cr.mediaKey !in localState.mediaReady) return "MEDIA_NOT_READY"
        return null
    }

    @Synchronized
    fun blockSegments(k: Long, external: Boolean): List<Segment> {
        val key = k * 2 + if (external) 1 else 0
        cache[key]?.let { return it }
        val built = if (external) buildExternal(k) else buildInternal(k)
        cache[key] = built
        return built
    }

    private fun buildInternal(k: Long): List<Segment> {
        val start = cycleStart(k)
        val end = start + i
        val out = ArrayList<Segment>()
        val pattern = manifest.internalPattern
        if (pattern.isEmpty()) {
            fill(start, end, Block.INTERNAL, out)
            return out
        }
        val seq = pattern[k.mod(pattern.size.toLong()).toInt()]
        var p = start
        for (entry in seq) {
            if (p >= end) break
            val itemEnd = p + entry.d
            if (itemEnd > end) {
                // item que ultrapassa o fim do bloco não toca; o resto do bloco vai para fallback
                fill(p, end, Block.INTERNAL, out)
                p = end
                break
            }
            if (ineligibility(entry.c, p) == null) {
                out += Segment(Block.INTERNAL, SegmentKind.ITEM, entry.c, p, itemEnd)
            } else {
                fill(p, itemEnd, Block.INTERNAL, out)
            }
            p = itemEnd
        }
        if (p < end) fill(p, end, Block.INTERNAL, out)
        return out
    }

    /** Bucket do plano externo para o bloco que começa em `s` (com repetição pelo horizonte); null = sem plano. */
    fun externalBucket(s: Long): Long? {
        val b = s.floorDiv(c) * c
        val h = manifest.horizon
        val p = h.repeatPeriodMs
        if (b >= h.fromMs && b < h.toMs) return b
        if (p <= 0) return null
        val candidate = if (b >= h.toMs) {
            val n = (b - h.toMs).floorDiv(p) + 1 // menor n ≥ 1 com b − n·P < to
            b - n * p
        } else {
            val m = -((b - h.fromMs).floorDiv(p)) // menor m ≥ 1 com b + m·P ≥ from (n = −m)
            b + m * p
        }
        return if (candidate >= h.fromMs && candidate < h.toMs) candidate else null
    }

    private fun buildExternal(k: Long): List<Segment> {
        val s = cycleStart(k) + i
        val end = cycleStart(k + 1)
        val out = ArrayList<Segment>()
        val bucket = externalBucket(s)
        val entries = bucket?.let { manifest.externalPlan[it] }.orEmpty()
            .withIndex().sortedWith(compareBy({ it.value.u }, { it.index })).map { it.value }
        var cursor = s
        for (e in entries) {
            val st = s + e.u * u
            val en = st + e.d
            if (st >= end) {
                // inteiramente fora do bloco: não toca e não há intervalo dentro do bloco
                continue
            }
            if (st > cursor) {
                fill(cursor, st, Block.EXTERNAL, out) // lacuna máxima sem entradas
                cursor = st
            }
            val overlaps = st < cursor
            val exceeds = en > end
            if (overlaps || exceeds) {
                // defesa (§8.2.4): não toca; intervalo recortado ao bloco (e ao que já foi ocupado) vai para fallback
                val fs = maxOf(st, cursor)
                val fe = minOf(en, end)
                if (fe > fs) {
                    fill(fs, fe, Block.EXTERNAL, out)
                    cursor = fe
                }
                continue
            }
            if (ineligibility(e.c, st) == null) {
                out += Segment(Block.EXTERNAL, SegmentKind.ITEM, e.c, st, en, insertionId = e.insertionId)
            } else {
                fill(st, en, Block.EXTERNAL, out)
            }
            cursor = en
        }
        if (cursor < end) fill(cursor, end, Block.EXTERNAL, out)
        return out
    }

    /**
     * Preenchimento por fallback de [s, e): ordem cíclica a partir de floor((s − A)/U) mod len; item só entra
     * se couber inteiro; o restante menor que o próximo item vira FILLER com `fallback[índice]` (sem PoP).
     * `fallback` vazio ⇒ FILLER com creative_key null. Fallback não passa por checagem de validade (§8.2.1).
     */
    fun fill(s: Long, e: Long, block: Block, out: MutableList<Segment>) {
        if (e <= s) return
        val fb = manifest.fallback
        if (fb.isEmpty()) {
            out += Segment(block, SegmentKind.FILLER, null, s, e, fromFallback = true)
            return
        }
        var idx = ((s - anchorMs).floorDiv(u)).mod(fb.size.toLong()).toInt()
        var p = s
        while (p < e) {
            val item = fb[idx]
            if (item.d > 0 && p + item.d <= e) {
                out += Segment(block, SegmentKind.ITEM, item.c, p, p + item.d, fromFallback = true)
                p += item.d
                idx = (idx + 1) % fb.size
            } else {
                out += Segment(block, SegmentKind.FILLER, item.c, p, e, fromFallback = true)
                p = e
            }
        }
    }

    /** Criativo de fallback usado como FILLER a partir de `t` (entrada no meio de item). */
    fun fillerCreativeAt(t: Long): String? {
        val fb = manifest.fallback
        if (fb.isEmpty()) return null
        return fb[((t - anchorMs).floorDiv(u)).mod(fb.size.toLong()).toInt()].c
    }

    companion object {
        /** Forma funcional usada pelos vetores de conformidade. */
        fun resolve(manifest: Manifest, anchorMs: Long, localState: LocalState, t: Long): Segment =
            CycleResolver(manifest, anchorMs, localState).resolve(t)
    }
}
