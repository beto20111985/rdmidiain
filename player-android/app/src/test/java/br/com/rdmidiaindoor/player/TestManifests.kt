package br.com.rdmidiaindoor.player

import br.com.rdmidiaindoor.player.protocol.JsonValue
import br.com.rdmidiaindoor.player.protocol.Manifest
import br.com.rdmidiaindoor.player.protocol.ManifestCodec
import br.com.rdmidiaindoor.player.protocol.jsonOf
import java.time.Instant

/** Construtor de manifestos RDPP/1 para testes (JSON real → [ManifestCodec.parse]). */
object TestManifests {
    const val SEC = 1_000L
    const val MIN = 60_000L
    const val DAY = 86_400_000L
    const val WEEK = 7 * DAY
    val FAR_FUTURE = 4_102_444_800_000L // 2100-01-01

    /** 08:07:00 em America/Sao_Paulo (UTC−3) de 2026-10-07. */
    val ANCHOR_0807: Long = Instant.parse("2026-10-07T11:07:00Z").toEpochMilli()

    fun local(hhmmss: String, date: String = "2026-10-07"): Long {
        val (h, m, s) = hhmmss.split(":").map { it.toInt() }
        return Instant.parse("${date}T%02d:%02d:%02dZ".format(h + 3, m, s)).toEpochMilli()
    }

    data class Creative(
        val key: String,
        val kind: String,
        val media: String,
        val durationMs: Long = 30_000,
        val validFrom: Long = 0,
        val validUntil: Long = FAR_FUTURE,
        val id: String = "id-$key",
        val code: String = "RD-${key.uppercase().padEnd(6, 'X').take(6)}",
    )

    class Builder(val anchorMs: Long?, val internalMs: Long = 120_000, val externalMs: Long = 120_000) {
        val creatives = LinkedHashMap<String, Creative>()
        val internalPattern = ArrayList<List<Pair<String, Long>>>()
        val fallback = ArrayList<Pair<String, Long>>()
        val externalPlan = LinkedHashMap<Long, List<JsonValue.Obj>>()
        val emergency = ArrayList<JsonValue.Obj>()
        val revoked = ArrayList<String>()
        var horizonFrom = 0L
        var horizonTo = FAR_FUTURE
        var repeatPeriod = WEEK
        var version = 42L
        var generatedAt = 0L

        val cycleMs get() = internalMs + externalMs

        fun creative(c: Creative) = apply { creatives[c.key] = c }

        fun bucketOfExternalStart(s: Long): Long = Math.floorDiv(s, cycleMs) * cycleMs

        /** Entradas externas para o bloco do ciclo k (âncora A). */
        fun external(k: Long, vararg entries: Triple<String, Long, Long>) = apply {
            val a = anchorMs ?: error("âncora necessária")
            val s = a + k * cycleMs + internalMs
            externalPlan[bucketOfExternalStart(s)] = entries.map { (c, u, d) -> jsonOf("i" to "ins-$c-$k-$u", "c" to c, "u" to u, "d" to d) }
        }

        fun externalAtBucket(bucket: Long, vararg entries: Triple<String, Long, Long>) = apply {
            externalPlan[bucket] = entries.map { (c, u, d) -> jsonOf("i" to "ins-$c-$u", "c" to c, "u" to u, "d" to d) }
        }

        fun json(): JsonValue.Obj {
            val media = LinkedHashMap<String, Any?>()
            creatives.values.map { it.media }.distinct().forEachIndexed { idx, m ->
                media[m] = jsonOf(
                    "media_id" to "mid-$m", "sha256" to "%064x".format(idx + 1), "size" to 1000L + idx,
                    "mime" to if (m.startsWith("v")) "video/mp4" else "image/jpeg", "width" to 1920, "height" to 1080, "duration_ms" to 30_000,
                )
            }
            val cr = LinkedHashMap<String, Any?>()
            creatives.values.forEach { c ->
                cr[c.key] = jsonOf(
                    "creative_id" to c.id, "code" to c.code, "kind" to c.kind, "campaign_id" to if (c.kind == "EXTERNAL") "camp-${c.key}" else null,
                    "advertiser_id" to null, "media" to c.media, "duration_ms" to c.durationMs, "valid_from_ms" to c.validFrom,
                    "valid_until_ms" to c.validUntil, "label" to ManifestCodec.defaultLabel(c.kind),
                )
            }
            return jsonOf(
                "schema" to "rd.manifest/1",
                "manifest_id" to "man-1",
                "version" to version,
                "screen_id" to "screen-1",
                "screen_code" to "TEL-GO-7Q3M",
                "generated_at_ms" to generatedAt,
                "timezone" to "America/Sao_Paulo",
                "rotation" to jsonOf("internal_ms" to internalMs, "external_ms" to externalMs, "cycle_ms" to cycleMs, "unit_ms" to 30_000L, "policy_id" to "pol-1"),
                "anchor_ms" to anchorMs,
                "horizon" to jsonOf("from_ms" to horizonFrom, "to_ms" to horizonTo, "repeat_period_ms" to repeatPeriod),
                "settings" to jsonOf(
                    "image_default_ms" to 30_000, "fade_ms" to 400, "orientation" to "LANDSCAPE",
                    "footer" to jsonOf("enabled" to true, "height_permille" to 60),
                    "offline_alert_days" to listOf(7, 28), "heartbeat_interval_ms" to 30_000, "telemetry_interval_ms" to 300_000,
                    "panel_url" to "https://painel.exemplo.test",
                ),
                "media" to media,
                "creatives" to cr,
                "internal_pattern" to internalPattern.map { seq -> seq.map { (c, d) -> jsonOf("c" to c, "d" to d) } },
                "fallback" to fallback.map { (c, d) -> jsonOf("c" to c, "d" to d) },
                "external_plan" to externalPlan.mapKeys { it.key.toString() },
                "emergency" to emergency,
                "revoked" to revoked,
            )
        }

        fun build(): Manifest = ManifestCodec.parse(json())
    }

    /** Cenário padrão 2+2 com âncora 08:07: interno c7/c8 (60 s cada), fallback f1 (30 s), externo e1/e2. */
    fun standard22(anchor: Long = ANCHOR_0807): Builder = Builder(anchor).apply {
        creative(Creative("c7", "INTERNAL", "i7", 60_000))
        creative(Creative("c8", "INTERNAL", "i8", 60_000))
        creative(Creative("f1", "FALLBACK", "if1", 30_000))
        creative(Creative("e1", "EXTERNAL", "ie1", 30_000))
        creative(Creative("e2", "EXTERNAL", "ve2", 60_000))
        creative(Creative("e3", "EXTERNAL", "ie3", 120_000))
        internalPattern += listOf("c7" to 60_000L, "c8" to 60_000L)
        fallback += "f1" to 30_000L
    }

    fun allMediaReady(m: Manifest): Set<String> = m.media.keys
}
