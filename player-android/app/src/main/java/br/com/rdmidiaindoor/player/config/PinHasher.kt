package br.com.rdmidiaindoor.player.config

import br.com.rdmidiaindoor.player.protocol.Base64Url
import java.security.MessageDigest
import java.security.SecureRandom
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * PIN técnico guardado apenas como hash PBKDF2-HMAC-SHA256 (`pbkdf2-sha256$<iter>$<salt b64u>$<hash b64u>`).
 * Implementação própria sobre `Mac("HmacSHA256")` porque `PBKDF2WithHmacSHA256` só existe na API 26+ (minSdk 24).
 * O mesmo formato pode ser gerado pelo painel/backend e enviado por provisionamento ou comando.
 */
object PinHasher {
    const val PREFIX = "pbkdf2-sha256"
    /** Mesmo custo do backend (RDPP §4 `set_tech_pin`: pbkdf2-sha256, 210 000 iterações, sal 16 B, hash 32 B). */
    const val DEFAULT_ITERATIONS = 210_000

    fun hash(pin: String, iterations: Int = DEFAULT_ITERATIONS, salt: ByteArray = ByteArray(16).also { SecureRandom().nextBytes(it) }): String {
        val dk = pbkdf2(pin.toByteArray(Charsets.UTF_8), salt, iterations, 32)
        return "$PREFIX\$$iterations\$${Base64Url.encode(salt)}\$${Base64Url.encode(dk)}"
    }

    fun verify(pin: String, encoded: String): Boolean {
        val parts = encoded.split('$')
        if (parts.size != 4 || parts[0] != PREFIX) return false
        val iter = parts[1].toIntOrNull() ?: return false
        if (iter < 1_000 || iter > 10_000_000) return false
        val salt = try { Base64Url.decode(parts[2]) } catch (e: Exception) { return false }
        val expected = try { Base64Url.decode(parts[3]) } catch (e: Exception) { return false }
        val dk = pbkdf2(pin.toByteArray(Charsets.UTF_8), salt, iter, expected.size)
        return MessageDigest.isEqual(dk, expected)
    }

    fun isValidEncoded(encoded: String): Boolean {
        val parts = encoded.split('$')
        return parts.size == 4 && parts[0] == PREFIX && (parts[1].toIntOrNull() ?: 0) >= 1_000 &&
            runCatching { Base64Url.decode(parts[2]).size >= 8 && Base64Url.decode(parts[3]).size >= 16 }.getOrDefault(false)
    }

    /** Mesma regra do backend: 6 a 12 dígitos. */
    fun isAcceptablePin(pin: String): Boolean = pin.length in 6..12 && pin.all { it in '0'..'9' }

    /**
     * Converte os `args` normativos do comando `set_tech_pin` (RDPP §4)
     * `{"algo":"pbkdf2-sha256","iterations":210000,"salt_b64u":"…","hash_b64u":"…"}` para o formato armazenado.
     * null = args inválidos (algoritmo diferente, iterações fora da faixa, sal < 16 B ou hash ≠ 32 B).
     */
    fun fromCommandArgs(algo: String?, iterations: Long?, saltB64u: String?, hashB64u: String?): String? {
        if (algo != PREFIX || iterations == null || iterations < 100_000 || iterations > 10_000_000) return null
        val salt = try { Base64Url.decode(saltB64u ?: return null) } catch (e: Exception) { return null }
        val hash = try { Base64Url.decode(hashB64u ?: return null) } catch (e: Exception) { return null }
        if (salt.size < 16 || hash.size != 32) return null
        return "$PREFIX\$$iterations\$${Base64Url.encode(salt)}\$${Base64Url.encode(hash)}"
    }

    /** PBKDF2 (RFC 8018 §5.2) com HMAC-SHA256. */
    fun pbkdf2(password: ByteArray, salt: ByteArray, iterations: Int, dkLen: Int): ByteArray {
        val mac = Mac.getInstance("HmacSHA256")
        // HMAC completa a chave com zeros: chave vazia ≡ chave 0x00 (SecretKeySpec não aceita chave vazia)
        mac.init(SecretKeySpec(if (password.isEmpty()) byteArrayOf(0) else password, "HmacSHA256"))
        val hLen = mac.macLength
        val blocks = (dkLen + hLen - 1) / hLen
        val out = ByteArray(dkLen)
        var pos = 0
        for (i in 1..blocks) {
            mac.update(salt)
            mac.update(byteArrayOf((i ushr 24).toByte(), (i ushr 16).toByte(), (i ushr 8).toByte(), i.toByte()))
            var u = mac.doFinal()
            val t = u.copyOf()
            for (j in 2..iterations) {
                u = mac.doFinal(u)
                for (k in t.indices) t[k] = (t[k].toInt() xor u[k].toInt()).toByte()
            }
            val n = minOf(hLen, dkLen - pos)
            System.arraycopy(t, 0, out, pos, n)
            pos += n
        }
        return out
    }
}
