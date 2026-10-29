package br.com.rdmidiaindoor.player.net

import br.com.rdmidiaindoor.player.protocol.AppUpdateOffer
import br.com.rdmidiaindoor.player.protocol.DeviceAuthPayloads
import br.com.rdmidiaindoor.player.protocol.DeviceSigner
import br.com.rdmidiaindoor.player.protocol.Json
import br.com.rdmidiaindoor.player.protocol.JsonValue
import br.com.rdmidiaindoor.player.protocol.ServerKey
import br.com.rdmidiaindoor.player.protocol.jsonOf
import br.com.rdmidiaindoor.player.sync.ManifestFetch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

data class PairingRequest(
    val requestId: String,
    val pairingCode: String,
    val expiresAtMs: Long,
    val panelPairUrl: String?,
    val wsPath: String?,
)

data class PairingResult(val deviceId: String, val screenId: String, val screenCode: String, val serverKeys: List<ServerKey>)

/** Endpoints `/api/v1/player/...` do RDPP/1. */
class PlayerApi(private val client: ApiClient) {

    suspend fun createPairingRequest(publicKeyB64u: String, appVersion: String, deviceInfo: Map<String, String>): PairingRequest {
        val body = jsonOf(
            "public_key" to publicKeyB64u,
            "key_alg" to "ES256",
            "platform" to "android",
            "app_version" to appVersion,
            "device_info" to deviceInfo,
        )
        val o = client.call("POST", "/api/v1/player/pairing/requests", Json.stringify(body)).json()
        return PairingRequest(
            requestId = o.str("pairing_request_id"),
            pairingCode = o.str("pairing_code"),
            expiresAtMs = o.long("expires_at_ms"),
            panelPairUrl = o.strOrNull("panel_pair_url"),
            wsPath = o.strOrNull("ws_path"),
        )
    }

    /** `{"status":"PENDING|CLAIMED|EXPIRED","expires_at_ms"}` (§8.2.6). */
    suspend fun pairingStatus(requestId: String): String =
        client.call("GET", "/api/v1/player/pairing/requests/$requestId").json().str("status")

    /** Técnico no dispositivo (login controlado). Credenciais nunca são armazenadas. */
    suspend fun claim(requestId: String, login: String, password: String, totp: String?, screenCode: String) {
        val body = jsonOf("login" to login, "password" to password, "totp" to totp?.ifBlank { null }, "screen_code" to screenCode)
        client.call("POST", "/api/v1/player/pairing/requests/$requestId/claim", Json.stringify(body))
    }

    /** Resultado assinado: headers `X-RD-Device-Ts-Ms` e `X-RD-Device-Signature` (§2.5, §8.2.5). */
    suspend fun pairingResult(requestId: String, signer: DeviceSigner, tsMs: Long): PairingResult {
        val sig = signer.signRdj1(DeviceAuthPayloads.pairingResult(requestId, tsMs))
        val o = client.call(
            "GET", "/api/v1/player/pairing/requests/$requestId/result",
            headers = mapOf("X-RD-Device-Ts-Ms" to tsMs.toString(), "X-RD-Device-Signature" to sig),
        ).json()
        return PairingResult(
            deviceId = o.str("device_id"),
            screenId = o.str("screen_id"),
            screenCode = o.str("screen_code"),
            serverKeys = o.arr("server_keys").map {
                val k = it as JsonValue.Obj
                ServerKey(k.str("key_id"), k.str("public_key"), k.strOrNull("alg") ?: "ES256")
            },
        )
    }

    suspend fun challenge(deviceId: String): String =
        client.call("POST", "/api/v1/player/auth/challenge", Json.stringify(jsonOf("device_id" to deviceId))).json().str("challenge")

    data class Token(val accessToken: String, val expiresInS: Long, val serverTimeMs: Long?)

    suspend fun token(deviceId: String, challenge: String, signer: DeviceSigner): Token {
        val signature = signer.signRdj1(DeviceAuthPayloads.auth(challenge, deviceId))
        val body = jsonOf("device_id" to deviceId, "challenge" to challenge, "signature" to signature)
        val o = client.call("POST", "/api/v1/player/auth/token", Json.stringify(body)).json()
        return Token(o.str("access_token"), o.longOrNull("expires_in") ?: 900L, o.longOrNull("server_time_ms"))
    }

    suspend fun wsTicket(bearer: String): String =
        client.call("POST", "/api/v1/player/ws-ticket", "{}", bearer = bearer).json().str("ticket")

    /** `If-None-Match: "<version>"` com aspas (§8.2.7). */
    suspend fun manifest(bearer: String, ifNoneMatchVersion: Long?): ManifestFetch {
        val headers = if (ifNoneMatchVersion != null) mapOf("If-None-Match" to "\"$ifNoneMatchVersion\"") else emptyMap()
        val r = client.call("GET", "/api/v1/player/manifest", bearer = bearer, headers = headers)
        return if (r.code == 304) ManifestFetch.NotModified else ManifestFetch.Body(r.body)
    }

    suspend fun postAnchor(bearer: String, anchorMs: Long): Long =
        client.call("POST", "/api/v1/player/anchor", Json.stringify(jsonOf("anchor_ms" to anchorMs)), bearer = bearer)
            .json().long("anchor_ms")

    suspend fun postEvents(bearer: String, body: String): String =
        String(client.call("POST", "/api/v1/player/events:batch", body, bearer = bearer).body, Charsets.UTF_8)

    suspend fun appUpdate(bearer: String, version: String): AppUpdateOffer? {
        val r = client.call("GET", "/api/v1/player/app-update", bearer = bearer,
            query = mapOf("platform" to "android", "version" to version))
        if (r.code == 204 || r.body.isEmpty()) return null
        return AppUpdateOffer.parse(r.json())
    }
}

/**
 * Autenticação do dispositivo (§3): desafio de uso único assinado → JWT de 15 min, renovado antes de expirar.
 * `DEVICE_REVOKED` ⇒ [onRevoked] (o runtime limpa a identidade e volta ao pareamento).
 */
class AuthManager(
    private val api: PlayerApi,
    private val signer: () -> DeviceSigner,
    private val deviceId: () -> String?,
    private val monoMs: () -> Long,
    private val onRevoked: () -> Unit,
) {
    private val mutex = Mutex()
    @Volatile private var token: String? = null
    @Volatile private var expiresAtMono: Long = 0

    fun invalidate() {
        token = null
    }

    suspend fun token(): String = mutex.withLock {
        val t = token
        if (t != null && monoMs() < expiresAtMono - 60_000L) return@withLock t
        val id = deviceId() ?: throw ApiException(401, "NOT_PAIRED", "dispositivo não pareado")
        try {
            val challenge = api.challenge(id)
            val tok = api.token(id, challenge, signer())
            token = tok.accessToken
            expiresAtMono = monoMs() + tok.expiresInS * 1000L
            tok.accessToken
        } catch (e: ApiException) {
            if (e.isDeviceRevoked) onRevoked()
            throw e
        }
    }

    /** Executa com Bearer; em 401 (não revogado) renova o token uma vez. */
    suspend fun <T> withAuth(block: suspend (String) -> T): T {
        val first = token()
        return try {
            block(first)
        } catch (e: ApiException) {
            if (e.isDeviceRevoked) {
                onRevoked()
                throw e
            }
            if (e.httpStatus != 401) throw e
            invalidate()
            block(token())
        }
    }
}
