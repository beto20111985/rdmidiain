package br.com.rdmidiaindoor.player.sync

import br.com.rdmidiaindoor.player.protocol.Manifest
import br.com.rdmidiaindoor.player.protocol.ManifestCodec
import br.com.rdmidiaindoor.player.protocol.ProtocolException
import br.com.rdmidiaindoor.player.protocol.ServerKey
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.delay
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.sync.withPermit
import java.util.concurrent.atomic.AtomicLong
import kotlin.random.Random

/** Resultado de `GET /api/v1/player/manifest` com `If-None-Match: "<version>"`. */
sealed class ManifestFetch {
    object NotModified : ManifestFetch()
    class Body(val bytes: ByteArray) : ManifestFetch()
}

fun interface ManifestFetcher {
    suspend fun fetch(ifNoneMatchVersion: Long?): ManifestFetch
}

enum class SyncState { RECEIVED, VERIFY_SIG, PLAN, DOWNLOADING, VERIFY_SHA, STAGED, COMMIT, ACTIVE, FAILED, IDLE }

interface SyncListener {
    fun onState(state: SyncState, version: Long?) {}
    fun onContact(nowMs: Long) {}
    /** `revoked` de um manifesto válido recém-recebido (aplicado na hora); [generatedAtMs] = geração desse manifesto. */
    fun onRevoked(creativeIds: Set<String>, generatedAtMs: Long) {}
    fun onProgress(doneBytes: Long, totalBytes: Long) {}
    fun onMediaError(mediaId: String, reason: String, attempts: Int) {}
    fun onManifestFailed(version: Long?, reason: String) {}
    fun onStaged(version: Long) {}
}

sealed class SyncOutcome {
    object UpToDate : SyncOutcome()
    data class Staged(val version: Long, val failedMedia: List<FailedMedia>) : SyncOutcome()
    data class Failed(val reason: String) : SyncOutcome()
}

/** Política de falha permanente de mídia: 5 tentativas em ≤ 30 min, backoff exponencial com jitter (RDPP §6). */
data class RetryPolicy(
    val maxAttempts: Int = 5,
    val windowMs: Long = 30 * 60_000L,
    val baseDelayMs: Long = 5_000L,
    val maxDelayMs: Long = 300_000L,
) {
    fun delayFor(attempt: Int, random: Random): Long {
        val exp = (baseDelayMs * Math.pow(3.0, (attempt - 1).toDouble())).toLong().coerceAtMost(maxDelayMs)
        return exp / 2 + random.nextLong(exp / 2 + 1)
    }
}

/**
 * Máquina de estados de sincronização (RDPP §6): RECEIVED → VERIFY_SIG → PLAN → DOWNLOADING → VERIFY_SHA → STAGED.
 * COMMIT/ACTIVE acontecem no runtime, na fronteira de ciclo ([GenerationStore.commitStaged]). Qualquer falha
 * mantém a versão ativa. Mídia com falha permanente ⇒ a versão é ativada sem ela (entradas viram fallback) + `media_error`.
 */
class SyncEngine(
    private val store: GenerationStore,
    private val fetcher: ManifestFetcher,
    private val downloader: MediaDownloader,
    private val serverKeys: () -> List<ServerKey>,
    private val expectedScreenId: () -> String?,
    private val listener: SyncListener,
    private val nowMs: () -> Long,
    private val monoMs: () -> Long,
    private val policy: RetryPolicy = RetryPolicy(),
    private val parallelDownloads: Int = 3,
    private val random: Random = Random.Default,
    private val sleep: suspend (Long) -> Unit = { delay(it) },
    private val freeBytes: () -> Long = { Long.MAX_VALUE },
    private val manifestOf: (Long) -> Manifest? = { null },
) {
    private val mutex = Mutex()

    @Volatile
    var state: SyncState = SyncState.IDLE
        private set

    val doneBytes = AtomicLong(0)
    val totalBytes = AtomicLong(0)

    private fun set(s: SyncState, v: Long?) {
        state = s
        listener.onState(s, v)
    }

    suspend fun sync(force: Boolean = false): SyncOutcome = mutex.withLock { syncLocked(force) }

    private suspend fun syncLocked(force: Boolean): SyncOutcome {
        val p = store.pointer()
        val known = p.staged ?: p.active
        val fetch = try {
            fetcher.fetch(if (force) null else known)
        } catch (e: Exception) {
            set(SyncState.IDLE, known)
            return SyncOutcome.Failed("FETCH: ${e.message ?: e.javaClass.simpleName}")
        }
        listener.onContact(nowMs())
        if (fetch is ManifestFetch.NotModified) {
            set(SyncState.IDLE, known)
            return SyncOutcome.UpToDate
        }
        val bytes = (fetch as ManifestFetch.Body).bytes
        set(SyncState.RECEIVED, null)

        // VERIFY_SIG
        set(SyncState.VERIFY_SIG, null)
        val env = try { ManifestCodec.parseEnvelope(bytes) } catch (e: ProtocolException) {
            return fail(null, "ENVELOPE_INVALID: ${e.code}")
        }
        if (!ManifestCodec.verify(env, serverKeys())) return fail(env.manifestJson.longOrNullSafe("version"), "SIGNATURE_INVALID")
        val manifest = try { ManifestCodec.parse(env.manifestJson) } catch (e: ProtocolException) {
            return fail(env.manifestJson.longOrNullSafe("version"), e.code)
        }
        val screen = expectedScreenId()
        if (screen != null && manifest.screenId != screen) return fail(manifest.version, "SCREEN_MISMATCH")
        val active = p.active
        if (active != null && manifest.version < active) return fail(manifest.version, "STALE_VERSION")
        // kill/revogação valem imediatamente, independentemente do download (ADR_007 §8)
        if (manifest.revoked.isNotEmpty()) listener.onRevoked(manifest.revoked, manifest.generatedAtMs)
        if (!force && (manifest.version == p.staged || (manifest.version == active && p.staged == null))) {
            set(SyncState.IDLE, manifest.version)
            return SyncOutcome.UpToDate
        }

        // PLAN
        set(SyncState.PLAN, manifest.version)
        val needed = manifest.media.values.distinctBy { it.sha256 }
        val missing = needed.filter { !store.hasVerified(it.sha256, it.size) }
        val missingBytes = missing.sumOf { it.size - store.partFile(it.sha256).let { f -> if (f.isFile) f.length() else 0L } }
        if (missingBytes > freeBytes()) {
            store.gc(nowMs(), manifestOf, needed.map { it.sha256 }.toSet())
            if (missingBytes > freeBytes()) return fail(manifest.version, "DISK_FULL")
        }
        totalBytes.set(missing.sumOf { it.size })
        doneBytes.set(0)

        // DOWNLOADING + VERIFY_SHA (2–3 em paralelo)
        set(SyncState.DOWNLOADING, manifest.version)
        val failedBySha = HashMap<String, Pair<String, Int>>()
        val sem = Semaphore(parallelDownloads)
        coroutineScope {
            missing.map { media ->
                async {
                    sem.withPermit {
                        val r = downloadWithRetry(media)
                        if (r != null) synchronized(failedBySha) { failedBySha[media.sha256] = r }
                    }
                }
            }.awaitAll()
        }
        set(SyncState.VERIFY_SHA, manifest.version)
        val ready = HashSet<String>()
        val failed = ArrayList<FailedMedia>()
        for (m in manifest.media.values) {
            val f = failedBySha[m.sha256]
            if (f == null && store.hasVerified(m.sha256, m.size)) {
                ready += m.key
            } else {
                val (reason, attempts) = f ?: ("MISSING_AFTER_DOWNLOAD" to 0)
                failed += FailedMedia(m.key, m.mediaId, reason, attempts)
            }
        }
        for (f in failed.distinctBy { it.mediaId }) listener.onMediaError(f.mediaId, f.reason, f.attempts)

        // STAGED
        store.stage(manifest.version, bytes, ready, failed, nowMs())
        set(SyncState.STAGED, manifest.version)
        listener.onStaged(manifest.version)
        return SyncOutcome.Staged(manifest.version, failed)
    }

    /** null = verificada; senão (motivo, tentativas) da falha permanente. */
    private suspend fun downloadWithRetry(media: Manifest.MediaRef): Pair<String, Int>? {
        val firstMono = monoMs()
        var attempt = 0
        var lastReason = "UNKNOWN"
        while (attempt < policy.maxAttempts) {
            attempt++
            var counted = 0L
            val outcome = downloader.download(media) { n ->
                counted += n
                listener.onProgress(doneBytes.addAndGet(n), totalBytes.get())
            }
            when (outcome) {
                is DownloadOutcome.Verified -> return null
                is DownloadOutcome.Failed -> {
                    lastReason = outcome.reason
                    doneBytes.addAndGet(-counted)
                    if (outcome.permanent) return lastReason to attempt
                }
            }
            if (attempt >= policy.maxAttempts) break
            val wait = policy.delayFor(attempt, random)
            if (monoMs() - firstMono + wait > policy.windowMs) break
            sleep(wait)
        }
        return lastReason to attempt
    }

    private fun fail(version: Long?, reason: String): SyncOutcome {
        set(SyncState.FAILED, version)
        listener.onManifestFailed(version, reason)
        return SyncOutcome.Failed(reason)
    }

    private fun br.com.rdmidiaindoor.player.protocol.JsonValue.Obj.longOrNullSafe(key: String): Long? =
        try { longOrNull(key) } catch (e: Exception) { null }
}
