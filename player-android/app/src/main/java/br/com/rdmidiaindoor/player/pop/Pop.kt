package br.com.rdmidiaindoor.player.pop

import br.com.rdmidiaindoor.player.protocol.Base64Url
import br.com.rdmidiaindoor.player.protocol.DeviceSigner
import br.com.rdmidiaindoor.player.protocol.Json
import br.com.rdmidiaindoor.player.protocol.JsonValue
import br.com.rdmidiaindoor.player.protocol.Rdj1
import br.com.rdmidiaindoor.player.protocol.jsonOf
import java.security.SecureRandom
import java.util.UUID

enum class PopResult { COMPLETED, INTERRUPTED, ERROR, SKIPPED }

/**
 * Regra de resultado (RDPP §7): COMPLETED somente se terminou integralmente — fim natural (vídeo: evento de fim
 * do decodificador; imagem: permanência completa), sem interrupção e com actual_ms ≥ expected_ms (sem tolerância).
 */
object PopRules {
    fun result(expectedMs: Long, actualMs: Long, endedNaturally: Boolean, interrupted: Boolean, error: Boolean): PopResult = when {
        error -> PopResult.ERROR
        !interrupted && endedNaturally && actualMs >= expectedMs -> PopResult.COMPLETED
        else -> PopResult.INTERRUPTED
    }
}

/** Dados de uma exibição (tempos em ms CORRIGIDOS: relógio local + offset — §7). */
data class PopInput(
    val creativeId: String,
    val creativeCode: String,
    val campaignId: String?,
    val insertionId: String?,
    val kind: String,
    val manifestVersion: Long,
    val startedAtMs: Long,
    val expectedMs: Long,
    val actualMs: Long,
    val result: PopResult,
    val clockOffsetMs: Long,
    val offline: Boolean,
) {
    val finishedAtMs: Long get() = startedAtMs + actualMs
}

data class ChainState(val deviceId: String, val lastSeq: Long, val lastHash: String) {
    companion object {
        val ZERO_HASH = "0".repeat(64)
        fun initial(deviceId: String) = ChainState(deviceId, 0L, ZERO_HASH)
    }
}

data class OutboxRow(
    val playbackUuid: String,
    val deviceId: String,
    val seq: Long,
    val eventJson: String,
    val signature: String,
    val createdAtMs: Long,
)

/**
 * Persistência do outbox + estado da cadeia (seq e prev_hash). [append] DEVE ser atômico: lê o estado,
 * grava o evento e o novo estado na mesma transação (Room no aparelho; memória nos testes).
 */
interface PopStore {
    fun append(deviceId: String, build: (ChainState) -> Pair<OutboxRow, ChainState>): OutboxRow
    fun chainState(deviceId: String): ChainState
    fun pending(limit: Int): List<OutboxRow>
    fun delete(playbackUuids: Collection<String>)
    fun count(): Int
}

class InMemoryPopStore : PopStore {
    private val chains = HashMap<String, ChainState>()
    private val rows = LinkedHashMap<String, OutboxRow>()

    @Synchronized
    override fun append(deviceId: String, build: (ChainState) -> Pair<OutboxRow, ChainState>): OutboxRow {
        val (row, next) = build(chains[deviceId] ?: ChainState.initial(deviceId))
        rows[row.playbackUuid] = row
        chains[deviceId] = next
        return row
    }

    @Synchronized
    override fun chainState(deviceId: String) = chains[deviceId] ?: ChainState.initial(deviceId)

    @Synchronized
    override fun pending(limit: Int) = rows.values.sortedBy { it.seq }.take(limit)

    @Synchronized
    override fun delete(playbackUuids: Collection<String>) { playbackUuids.forEach { rows.remove(it) } }

    @Synchronized
    override fun count() = rows.size
}

/** Monta, encadeia (seq/prev_hash), assina e grava eventos PoP no outbox (RDPP §7). */
class PopRecorder(
    private val store: PopStore,
    private val signer: DeviceSigner,
    private val random: SecureRandom = SecureRandom(),
    private val uuid: () -> String = { UUID.randomUUID().toString() },
    private val nowMs: () -> Long = { System.currentTimeMillis() },
) {
    fun record(deviceId: String, screenId: String, input: PopInput): OutboxRow = store.append(deviceId) { chain ->
        val seq = chain.lastSeq + 1
        val nonce = ByteArray(16).also { random.nextBytes(it) }
        val playbackUuid = uuid()
        val event = eventJson(deviceId, screenId, input, playbackUuid, seq, Base64Url.encode(nonce), chain.lastHash)
        val canonical = Rdj1.encode(event)
        val bytes = canonical.toByteArray(Charsets.UTF_8)
        val signature = signer.signB64u(bytes)
        val hash = br.com.rdmidiaindoor.player.protocol.Hex.encode(Rdj1.sha256(bytes))
        OutboxRow(playbackUuid, deviceId, seq, canonical, signature, nowMs()) to ChainState(deviceId, seq, hash)
    }

    companion object {
        fun eventJson(
            deviceId: String, screenId: String, i: PopInput, playbackUuid: String, seq: Long, nonce: String, prevHash: String,
        ): JsonValue.Obj = jsonOf(
            "schema" to "rd.pop/1",
            "playback_uuid" to playbackUuid,
            "device_id" to deviceId,
            "screen_id" to screenId,
            "creative_id" to i.creativeId,
            "creative_code" to i.creativeCode,
            "campaign_id" to i.campaignId,
            "insertion_id" to i.insertionId,
            "kind" to i.kind,
            "manifest_version" to i.manifestVersion,
            "started_at_ms" to i.startedAtMs,
            "finished_at_ms" to i.finishedAtMs,
            "expected_ms" to i.expectedMs,
            "actual_ms" to i.actualMs,
            "result" to i.result.name,
            "seq" to seq,
            "nonce" to nonce,
            "prev_hash" to prevHash,
            "clock_offset_ms" to i.clockOffsetMs,
            "offline" to i.offline,
        )

        /** Corpo de `POST /api/v1/player/events:batch` (≤ 200 por lote), reaproveitando o RDJ1 gravado. */
        fun batchBody(rows: List<OutboxRow>): String = buildString {
            append("{\"events\":[")
            rows.forEachIndexed { idx, r ->
                if (idx > 0) append(',')
                append("{\"event\":").append(r.eventJson).append(",\"signature\":")
                Rdj1.appendString(this, r.signature)
                append('}')
            }
            append("]}")
        }
    }
}

/** Envio do outbox em lotes com confirmação (remove somente eventos com status definitivo). */
class PopUploader(
    private val store: PopStore,
    private val post: suspend (body: String) -> String,
    private val batchSize: Int = 200,
) {
    data class FlushResult(val sent: Int, val removed: Int, val lastAckSeq: Long?)

    suspend fun flush(maxBatches: Int = 50): FlushResult {
        var sent = 0
        var removed = 0
        var ack: Long? = null
        repeat(maxBatches) {
            val rows = store.pending(batchSize)
            if (rows.isEmpty()) return FlushResult(sent, removed, ack)
            val response = Json.parseObject(post(PopRecorder.batchBody(rows)))
            sent += rows.size
            val known = rows.map { it.playbackUuid }.toSet()
            val definitive = response.arrOrEmpty("results").mapNotNull { r ->
                val o = r as? JsonValue.Obj ?: return@mapNotNull null
                val status = o.strOrNull("status")
                val id = o.strOrNull("playback_uuid")
                if (id != null && id in known && status in DEFINITIVE) id else null
            }
            store.delete(definitive)
            removed += definitive.size
            ack = response.longOrNull("ack_upto_seq") ?: ack
            if (definitive.isEmpty()) return FlushResult(sent, removed, ack) // evita laço sem progresso
        }
        return FlushResult(sent, removed, ack)
    }

    companion object {
        val DEFINITIVE = setOf("ACCEPTED", "DUPLICATE", "REJECTED")
    }
}
