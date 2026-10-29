package br.com.rdmidiaindoor.player.protocol

/**
 * Resposta de `GET /api/v1/player/app-update` (RDPP §8):
 * `{"version","url","sha256","size","signature","key_id","mandatory"}`. A assinatura é ES256 com a CHAVE DE RELEASE
 * (distinta da chave de manifesto) sobre RDJ1 `{"platform","purpose":"rd-app-update-v1","sha256","size","version"}`
 * — a `url` fica fora da assinatura. O SPKI da chave de release é fixado na configuração do player.
 */
data class AppUpdateOffer(
    val version: String,
    val url: String,
    val sha256: String,
    val size: Long,
    val signature: String,
    val keyId: String,
    val mandatory: Boolean,
) {
    fun signedPayload(platform: String): JsonValue.Obj = jsonOf(
        "platform" to platform,
        "purpose" to PURPOSE,
        "sha256" to sha256,
        "size" to size,
        "version" to version,
    )

    /** [releaseKeys] = mapa key_id → SPKI base64url (fixado no build/config). */
    fun verify(platform: String, releaseKeys: Map<String, String>): Boolean {
        if (!Hex.isSha256Hex(sha256) || size <= 0) return false
        val spki = releaseKeys[keyId] ?: return false
        return Es256.verifyB64u(spki, Rdj1.bytes(signedPayload(platform)), signature)
    }

    companion object {
        const val PURPOSE = "rd-app-update-v1"

        fun parse(o: JsonValue.Obj) = AppUpdateOffer(
            version = o.str("version"),
            url = o.str("url"),
            sha256 = o.str("sha256").lowercase(),
            size = o.long("size"),
            signature = o.str("signature"),
            keyId = o.str("key_id"),
            mandatory = o.boolOrNull("mandatory") ?: false,
        )

        /** Formato da config: `keyId=spkiB64u;keyId2=spki2`. */
        fun parseKeys(config: String): Map<String, String> = config.split(';')
            .map { it.trim() }
            .filter { it.contains('=') }
            .associate { it.substringBefore('=').trim() to it.substringAfter('=').trim() }
    }
}

/** Mensagens assinadas pelo dispositivo (RDPP §2.5 e §3). */
object DeviceAuthPayloads {
    const val AUTH_PURPOSE = "rd-player-auth-v1"
    const val PAIRING_RESULT_PURPOSE = "rd-pairing-result-v1"

    fun auth(challenge: String, deviceId: String) =
        jsonOf("challenge" to challenge, "device_id" to deviceId, "purpose" to AUTH_PURPOSE)

    fun pairingResult(pairingRequestId: String, tsMs: Long) =
        jsonOf("pairing_request_id" to pairingRequestId, "purpose" to PAIRING_RESULT_PURPOSE, "ts_ms" to tsMs)
}
