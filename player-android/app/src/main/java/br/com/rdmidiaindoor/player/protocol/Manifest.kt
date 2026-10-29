package br.com.rdmidiaindoor.player.protocol

/** Modelo do manifesto RDPP/1 §5 (`schema = rd.manifest/1`). Todos os tempos em ms UTC. */
data class Manifest(
    val schema: String,
    val manifestId: String,
    val version: Long,
    val screenId: String,
    val screenCode: String,
    val generatedAtMs: Long,
    val timezone: String,
    val rotation: Rotation,
    val anchorMs: Long?,
    val horizon: Horizon,
    val settings: Settings,
    val media: Map<String, MediaRef>,
    val creatives: Map<String, CreativeRef>,
    val internalPattern: List<List<SeqEntry>>,
    val fallback: List<SeqEntry>,
    val externalPlan: Map<Long, List<ExternalEntry>>,
    val emergency: List<EmergencyEntry>,
    val revoked: Set<String>,
) {
    data class Rotation(val internalMs: Long, val externalMs: Long, val cycleMs: Long, val unitMs: Long, val policyId: String?)
    data class Horizon(val fromMs: Long, val toMs: Long, val repeatPeriodMs: Long)
    data class Footer(val enabled: Boolean, val heightPermille: Int)
    data class Settings(
        val imageDefaultMs: Long,
        val fadeMs: Long,
        val orientation: String,
        val footer: Footer,
        val offlineAlertDays: List<Int>,
        val heartbeatIntervalMs: Long,
        val telemetryIntervalMs: Long,
        val panelUrl: String?,
    )
    data class MediaRef(
        val key: String,
        val mediaId: String,
        val sha256: String,
        val size: Long,
        val mime: String,
        val width: Long?,
        val height: Long?,
        val durationMs: Long?,
    ) {
        val isVideo: Boolean get() = mime.startsWith("video/")
    }
    data class CreativeRef(
        val key: String,
        val creativeId: String,
        val code: String,
        val kind: String,
        val campaignId: String?,
        val advertiserId: String?,
        val mediaKey: String,
        val durationMs: Long,
        val validFromMs: Long,
        val validUntilMs: Long,
        val label: String,
    )
    data class SeqEntry(val c: String, val d: Long)
    data class ExternalEntry(val insertionId: String?, val c: String, val u: Long, val d: Long)
    data class EmergencyEntry(val id: String, val c: String, val fromMs: Long, val untilMs: Long)

    companion object {
        const val SCHEMA = "rd.manifest/1"
        const val HOUR_MS = 3_600_000L
    }
}

/** Envelope assinado `{"alg","key_id","signature","manifest"}`. */
data class ManifestEnvelope(
    val alg: String,
    val keyId: String,
    val signature: String,
    val manifestJson: JsonValue.Obj,
)

data class ServerKey(val keyId: String, val publicKeyB64u: String, val alg: String)

object ManifestCodec {

    fun parseEnvelope(bytes: ByteArray): ManifestEnvelope {
        val o = Json.parseObject(bytes)
        return ManifestEnvelope(
            alg = o.str("alg"),
            keyId = o.str("key_id"),
            signature = o.str("signature"),
            manifestJson = o.obj("manifest"),
        )
    }

    /** VERIFY_SIG: ES256 com a chave `key_id` recebida no pareamento, sobre RDJ1(manifest). */
    fun verify(env: ManifestEnvelope, keys: List<ServerKey>): Boolean {
        if (env.alg != "ES256") return false
        val key = keys.firstOrNull { it.keyId == env.keyId && it.alg == "ES256" } ?: return false
        val data = try { Rdj1.bytes(env.manifestJson) } catch (e: ProtocolException) { return false }
        return Es256.verifyB64u(key.publicKeyB64u, data, env.signature)
    }

    fun parse(o: JsonValue.Obj): Manifest {
        val schema = o.str("schema")
        if (schema != Manifest.SCHEMA) throw ProtocolException("MANIFEST_SCHEMA", "schema não suportado: $schema")
        // Garante que o objeto é representável em RDJ1 (sem floats etc.).
        Rdj1.encode(o)

        val rot = o.obj("rotation")
        val rotation = Manifest.Rotation(
            internalMs = rot.long("internal_ms"),
            externalMs = rot.long("external_ms"),
            cycleMs = rot.long("cycle_ms"),
            unitMs = rot.long("unit_ms"),
            policyId = rot.strOrNull("policy_id"),
        )
        val hz = o.obj("horizon")
        val horizon = Manifest.Horizon(hz.long("from_ms"), hz.long("to_ms"), hz.long("repeat_period_ms"))

        val st = o.obj("settings")
        val footer = st.objOrNull("footer")
        val settings = Manifest.Settings(
            imageDefaultMs = st.longOrNull("image_default_ms") ?: 30_000L,
            fadeMs = st.longOrNull("fade_ms") ?: 400L,
            orientation = st.strOrNull("orientation") ?: "LANDSCAPE",
            footer = Manifest.Footer(
                enabled = footer?.boolOrNull("enabled") ?: true,
                heightPermille = (footer?.longOrNull("height_permille") ?: 60L).toInt(),
            ),
            offlineAlertDays = st.arrOrEmpty("offline_alert_days").map { (it as JsonValue.Num).toLong().toInt() }
                .ifEmpty { listOf(7, 28) },
            heartbeatIntervalMs = st.longOrNull("heartbeat_interval_ms") ?: 30_000L,
            telemetryIntervalMs = st.longOrNull("telemetry_interval_ms") ?: 300_000L,
            panelUrl = st.strOrNull("panel_url"),
        )

        val media = LinkedHashMap<String, Manifest.MediaRef>()
        for ((k, v) in o.obj("media").fields) {
            val m = v as? JsonValue.Obj ?: throw ProtocolException("BAD_TYPE", "media.$k")
            val sha = m.str("sha256").lowercase()
            if (!Hex.isSha256Hex(sha)) throw ProtocolException("MANIFEST_INVALID", "sha256 inválido em media.$k")
            media[k] = Manifest.MediaRef(
                key = k, mediaId = m.str("media_id"), sha256 = sha, size = m.long("size"), mime = m.str("mime"),
                width = m.longOrNull("width"), height = m.longOrNull("height"), durationMs = m.longOrNull("duration_ms"),
            )
        }

        val creatives = LinkedHashMap<String, Manifest.CreativeRef>()
        for ((k, v) in o.obj("creatives").fields) {
            val c = v as? JsonValue.Obj ?: throw ProtocolException("BAD_TYPE", "creatives.$k")
            creatives[k] = Manifest.CreativeRef(
                key = k,
                creativeId = c.str("creative_id"),
                code = c.str("code"),
                kind = c.str("kind"),
                campaignId = c.strOrNull("campaign_id"),
                advertiserId = c.strOrNull("advertiser_id"),
                mediaKey = c.str("media"),
                durationMs = c.long("duration_ms"),
                validFromMs = c.long("valid_from_ms"),
                validUntilMs = c.long("valid_until_ms"),
                label = c.strOrNull("label") ?: defaultLabel(c.str("kind")),
            )
        }

        fun seq(list: List<JsonValue>): List<Manifest.SeqEntry> = list.map {
            val e = it as? JsonValue.Obj ?: throw ProtocolException("BAD_TYPE", "entrada de sequência")
            Manifest.SeqEntry(e.str("c"), e.long("d"))
        }

        val internalPattern = o.arr("internal_pattern").map {
            seq((it as? JsonValue.Arr)?.items ?: throw ProtocolException("BAD_TYPE", "internal_pattern"))
        }
        val fallback = seq(o.arr("fallback"))

        val externalPlan = HashMap<Long, List<Manifest.ExternalEntry>>()
        for ((k, v) in o.obj("external_plan").fields) {
            val bucket = k.toLongOrNull() ?: throw ProtocolException("MANIFEST_INVALID", "chave de bucket inválida: $k")
            val entries = ((v as? JsonValue.Arr)?.items ?: throw ProtocolException("BAD_TYPE", "external_plan.$k")).map {
                val e = it as? JsonValue.Obj ?: throw ProtocolException("BAD_TYPE", "external_plan.$k[]")
                Manifest.ExternalEntry(e.strOrNull("i"), e.str("c"), e.long("u"), e.long("d"))
            }
            externalPlan[bucket] = entries
        }

        val emergency = o.arr("emergency").map {
            val e = it as? JsonValue.Obj ?: throw ProtocolException("BAD_TYPE", "emergency[]")
            Manifest.EmergencyEntry(e.str("id"), e.str("c"), e.long("from_ms"), e.long("until_ms"))
        }
        val revoked = o.arr("revoked").map { (it as? JsonValue.Str)?.value ?: throw ProtocolException("BAD_TYPE", "revoked[]") }.toSet()

        val manifest = Manifest(
            schema = schema,
            manifestId = o.str("manifest_id"),
            version = o.long("version"),
            screenId = o.str("screen_id"),
            screenCode = o.str("screen_code"),
            generatedAtMs = o.long("generated_at_ms"),
            timezone = o.str("timezone"),
            rotation = rotation,
            anchorMs = o.longOrNull("anchor_ms"),
            horizon = horizon,
            settings = settings,
            media = media,
            creatives = creatives,
            internalPattern = internalPattern,
            fallback = fallback,
            externalPlan = externalPlan,
            emergency = emergency,
            revoked = revoked,
        )
        val errors = validate(manifest)
        if (errors.isNotEmpty()) throw ProtocolException("MANIFEST_INVALID", errors.joinToString("; "))
        return manifest
    }

    fun parse(bytes: ByteArray): Manifest = parse(Json.parseObject(bytes))

    /** Regras estruturais do gerador (§5). Erros aqui ⇒ `manifest_failed` (a versão ativa é mantida). */
    fun validate(m: Manifest): List<String> {
        val e = ArrayList<String>()
        val r = m.rotation
        if (r.unitMs <= 0) e += "unit_ms deve ser > 0"
        if (r.cycleMs <= 0 || Manifest.HOUR_MS % r.cycleMs != 0L) e += "cycle_ms deve dividir 3 600 000"
        if (r.internalMs < 0 || r.externalMs < 0 || r.internalMs + r.externalMs != r.cycleMs) e += "internal_ms + external_ms deve ser cycle_ms"
        if (r.unitMs > 0 && (r.internalMs % r.unitMs != 0L || r.externalMs % r.unitMs != 0L)) e += "blocos devem ser múltiplos de unit_ms"
        if (m.horizon.repeatPeriodMs <= 0) e += "repeat_period_ms deve ser > 0"
        if (m.horizon.toMs < m.horizon.fromMs) e += "horizonte inválido"
        for (c in m.creatives.values) {
            if (!m.media.containsKey(c.mediaKey)) e += "criativo ${c.key} referencia mídia inexistente ${c.mediaKey}"
            if (c.durationMs <= 0) e += "criativo ${c.key} com duração inválida"
        }
        fun checkSeq(where: String, list: List<Manifest.SeqEntry>) = list.forEach {
            if (!m.creatives.containsKey(it.c)) e += "$where referencia criativo inexistente ${it.c}"
            if (it.d <= 0) e += "$where com duração inválida"
        }
        m.internalPattern.forEachIndexed { i, s -> checkSeq("internal_pattern[$i]", s) }
        checkSeq("fallback", m.fallback)
        for ((b, list) in m.externalPlan) list.forEach {
            if (!m.creatives.containsKey(it.c)) e += "external_plan[$b] referencia criativo inexistente ${it.c}"
            if (it.d <= 0 || it.u < 0) e += "external_plan[$b] com u/d inválidos"
        }
        m.emergency.forEach { if (!m.creatives.containsKey(it.c)) e += "emergency ${it.id} referencia criativo inexistente ${it.c}" }
        return e
    }

    fun defaultLabel(kind: String): String = when (kind) {
        "INTERNAL" -> "Conteúdo do estabelecimento"
        "FALLBACK" -> "RD Mídia Indoor"
        else -> "Publicidade"
    }
}
