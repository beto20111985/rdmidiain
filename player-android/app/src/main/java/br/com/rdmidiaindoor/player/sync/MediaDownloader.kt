package br.com.rdmidiaindoor.player.sync

import br.com.rdmidiaindoor.player.protocol.Hex
import br.com.rdmidiaindoor.player.protocol.Manifest
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.ensureActive
import java.io.Closeable
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.InputStream
import java.security.MessageDigest

/** Resposta de mídia: [code] HTTP; [rangeStart] início do Content-Range (206); [body] stream (fechado pelo chamador). */
class MediaResponse(val code: Int, val rangeStart: Long?, val body: InputStream?) : Closeable {
    override fun close() {
        try { body?.close() } catch (_: Exception) {}
    }
}

/** Fonte de mídia (HTTP com Bearer no aparelho; fake nos testes). */
fun interface MediaSource {
    /** GET da mídia a partir de [offset] (Range) condicionado a If-Range com o ETag `"<sha256>"`. */
    suspend fun open(mediaId: String, offset: Long, sha256: String): MediaResponse
}

sealed class DownloadOutcome {
    data class Verified(val file: File) : DownloadOutcome()
    /** [permanent] = não adianta repetir (ex.: 404/410); senão conta como tentativa e repete com backoff. */
    data class Failed(val reason: String, val permanent: Boolean = false) : DownloadOutcome()
}

/**
 * Download retomável com verificação incremental (RDPP §6): Range/If-Range, SHA-256 em stream com
 * [MessageDigest] (nunca o arquivo inteiro em memória), promoção para `media/<sha256>` só após conferir.
 */
class MediaDownloader(
    private val store: GenerationStore,
    private val source: MediaSource,
    private val bufferSize: Int = 64 * 1024,
) {
    suspend fun download(media: Manifest.MediaRef, onBytes: (Long) -> Unit = {}): DownloadOutcome {
        if (store.hasVerified(media.sha256, media.size)) return DownloadOutcome.Verified(store.mediaFile(media.sha256))
        val part = store.partFile(media.sha256)
        part.parentFile?.mkdirs()
        if (part.length() > media.size) part.delete()

        val digest = MessageDigest.getInstance("SHA-256")
        var offset = 0L
        if (part.isFile && part.length() > 0) {
            // retomada: o estado do digest é reconstruído relendo o parcial em stream
            FileInputStream(part).use { input -> offset = feed(input, digest, null) }
        }

        val response = try {
            source.open(media.mediaId, offset, media.sha256)
        } catch (e: Exception) {
            return DownloadOutcome.Failed("NETWORK: ${e.javaClass.simpleName}")
        }
        response.use { resp ->
            var append = false
            when (resp.code) {
                206 -> {
                    if (resp.rangeStart != offset) {
                        part.delete()
                        return DownloadOutcome.Failed("RANGE_MISMATCH")
                    }
                    append = true
                }
                200 -> {
                    // servidor ignorou Range ou o ETag mudou (If-Range): recomeça do zero
                    digest.reset()
                    offset = 0
                }
                416 -> {
                    if (offset == media.size) return finish(media, part, digest)
                    part.delete()
                    return DownloadOutcome.Failed("RANGE_NOT_SATISFIABLE")
                }
                401 -> return DownloadOutcome.Failed("UNAUTHORIZED")
                403, 404, 410 -> return DownloadOutcome.Failed("HTTP_${resp.code}", permanent = resp.code != 403)
                else -> return DownloadOutcome.Failed("HTTP_${resp.code}")
            }
            val body = resp.body ?: return DownloadOutcome.Failed("EMPTY_BODY")
            var total = offset
            try {
                FileOutputStream(part, append).use { out ->
                    val buf = ByteArray(bufferSize)
                    while (true) {
                        currentCoroutineContext().ensureActive()
                        val n = body.read(buf)
                        if (n < 0) break
                        total += n
                        if (total > media.size) {
                            out.close()
                            part.delete()
                            return DownloadOutcome.Failed("SIZE_EXCEEDED")
                        }
                        out.write(buf, 0, n)
                        digest.update(buf, 0, n)
                        onBytes(n.toLong())
                    }
                    out.flush()
                    out.fd.sync()
                }
            } catch (e: Exception) {
                if (e is kotlinx.coroutines.CancellationException) throw e
                return DownloadOutcome.Failed("IO: ${e.javaClass.simpleName}") // parcial mantido para retomar
            }
            if (total < media.size) return DownloadOutcome.Failed("INCOMPLETE")
            return finish(media, part, digest)
        }
    }

    private fun finish(media: Manifest.MediaRef, part: File, digest: MessageDigest): DownloadOutcome {
        val sha = Hex.encode(digest.digest())
        if (sha != media.sha256 || part.length() != media.size) {
            part.delete()
            return DownloadOutcome.Failed("SHA256_MISMATCH")
        }
        return DownloadOutcome.Verified(store.adoptVerified(part, media.sha256))
    }

    private fun feed(input: InputStream, digest: MessageDigest, onBytes: ((Long) -> Unit)?): Long {
        val buf = ByteArray(bufferSize)
        var total = 0L
        while (true) {
            val n = input.read(buf)
            if (n < 0) break
            digest.update(buf, 0, n)
            total += n
            onBytes?.invoke(n.toLong())
        }
        return total
    }
}
