package br.com.rdmidiaindoor.player.net

import br.com.rdmidiaindoor.player.core.ClockService
import br.com.rdmidiaindoor.player.protocol.Json
import br.com.rdmidiaindoor.player.protocol.JsonValue
import br.com.rdmidiaindoor.player.sync.MediaResponse
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.IOException
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/** Erro HTTP com o envelope do contrato `{"error":{"code","message","details"}}`. */
class ApiException(val httpStatus: Int, val code: String, message: String) : IOException("HTTP $httpStatus $code: $message") {
    val isDeviceRevoked: Boolean get() = code == "DEVICE_REVOKED"
}

/**
 * Política de URL base (sem domínio hardcoded): HTTPS obrigatório; HTTP somente para hosts de desenvolvimento
 * configurados explicitamente no build (mesma lista do network_security_config gerado).
 */
object UrlPolicy {
    fun validate(baseUrl: String, cleartextHosts: Set<String>): String? {
        val url = baseUrl.trim().trimEnd('/').toHttpUrlOrNull() ?: return "URL inválida"
        // prefixo de caminho é permitido (API atrás de proxy), mas sem query/fragment
        if (url.query != null || url.fragment != null) return "URL não pode ter query/fragment"
        if (url.scheme == "https") return null
        if (url.scheme == "http" && url.host in cleartextHosts) return null
        return "HTTP só é permitido para hosts de desenvolvimento configurados (${cleartextHosts.joinToString()})"
    }

    fun normalize(baseUrl: String): String = baseUrl.trim().trimEnd('/')

    /** ws(s)://host[:porta][prefixo]/<path> a partir da URL base http(s). */
    fun wsUrl(baseUrl: String, path: String, query: Map<String, String>): String {
        val base = normalize(baseUrl).toHttpUrlOrNull() ?: throw IllegalArgumentException("URL base inválida")
        val b: HttpUrl.Builder = base.newBuilder()
        path.trim('/').split('/').filter { it.isNotEmpty() }.forEach { b.addPathSegment(it) }
        query.forEach { (k, v) -> b.addQueryParameter(k, v) }
        val http = b.build().toString()
        return if (http.startsWith("https://")) "wss://" + http.removePrefix("https://") else "ws://" + http.removePrefix("http://")
    }
}

data class ApiResponse(val code: Int, val body: ByteArray, val headers: Map<String, String>) {
    fun json(): JsonValue.Obj = Json.parseObject(body)
}

/**
 * Cliente HTTP do RDPP (OkHttp). Toda resposta de `/api/v1/player` traz `X-Server-Time-Ms`, usado para medir o
 * offset do relógio. Tokens só em header (nunca em URL).
 */
class ApiClient(
    private val http: OkHttpClient,
    private val baseUrl: () -> String,
    private val clock: ClockService,
) {
    private val jsonType = "application/json; charset=utf-8".toMediaType()

    fun url(path: String, query: Map<String, String> = emptyMap()): HttpUrl {
        val base = UrlPolicy.normalize(baseUrl()).toHttpUrlOrNull() ?: throw IOException("URL base não configurada")
        val b = base.newBuilder()
        path.trim('/').split('/').filter { it.isNotEmpty() }.forEach { b.addPathSegment(it) }
        query.forEach { (k, v) -> b.addQueryParameter(k, v) }
        return b.build()
    }

    suspend fun call(
        method: String,
        path: String,
        body: String? = null,
        bearer: String? = null,
        headers: Map<String, String> = emptyMap(),
        query: Map<String, String> = emptyMap(),
    ): ApiResponse {
        val rb = Request.Builder().url(url(path, query))
        headers.forEach { (k, v) -> rb.header(k, v) }
        if (bearer != null) rb.header("Authorization", "Bearer $bearer")
        rb.header("Accept", "application/json")
        val reqBody = body?.toRequestBody(jsonType)
        rb.method(method, if (method == "GET" || method == "HEAD") null else (reqBody ?: "".toRequestBody(jsonType)))
        val sent = clock.localNow()
        val resp = execute(rb.build())
        resp.use { r ->
            val received = clock.localNow()
            r.header("X-Server-Time-Ms")?.toLongOrNull()?.let { clock.onServerTime(it, sent, received) }
            val bytes = withContext(Dispatchers.IO) { r.body?.bytes() ?: ByteArray(0) }
            val hdrs = r.headers.names().associateWith { r.header(it) ?: "" }
            if (r.code >= 400) throw toApiException(r.code, bytes)
            return ApiResponse(r.code, bytes, hdrs)
        }
    }

    /** GET de mídia em stream com Range/If-Range (ETag = `"<sha256>"`, com aspas — §8.2.8). */
    suspend fun openMedia(mediaId: String, offset: Long, sha256: String, bearer: String): MediaResponse {
        val rb = Request.Builder().url(url("/api/v1/player/media/$mediaId"))
            .header("Authorization", "Bearer $bearer")
        if (offset > 0) {
            rb.header("Range", "bytes=$offset-")
            rb.header("If-Range", "\"$sha256\"")
        }
        val sent = clock.localNow()
        val r = execute(rb.get().build())
        r.header("X-Server-Time-Ms")?.toLongOrNull()?.let { clock.onServerTime(it, sent, clock.localNow()) }
        if (r.code >= 400) {
            r.close()
            return MediaResponse(r.code, null, null)
        }
        val start = r.header("Content-Range")?.let { parseContentRangeStart(it) }
        return MediaResponse(r.code, if (r.code == 206) start else null, r.body?.byteStream())
    }

    private suspend fun execute(request: Request): Response = suspendCancellableCoroutine { cont ->
        val call = http.newCall(request)
        cont.invokeOnCancellation { call.cancel() }
        call.enqueue(object : Callback {
            override fun onFailure(call: Call, e: IOException) {
                if (cont.isActive) cont.resumeWithException(e)
            }

            override fun onResponse(call: Call, response: Response) {
                if (cont.isActive) cont.resume(response) else response.close()
            }
        })
    }

    companion object {
        fun parseContentRangeStart(v: String): Long? =
            Regex("""bytes\s+(\d+)-(\d+)/(\d+|\*)""").find(v)?.groupValues?.get(1)?.toLongOrNull()

        fun toApiException(status: Int, body: ByteArray): ApiException {
            return try {
                val err = Json.parseObject(body).obj("error")
                ApiException(status, err.strOrNull("code") ?: "HTTP_$status", err.strOrNull("message") ?: "")
            } catch (e: Exception) {
                ApiException(status, "HTTP_$status", "")
            }
        }
    }
}
