package br.com.rdmidiaindoor.player.protocol

import java.math.BigInteger
import java.security.KeyFactory
import java.security.KeyPair
import java.security.KeyPairGenerator
import java.security.PrivateKey
import java.security.PublicKey
import java.security.Signature
import java.security.interfaces.ECPublicKey
import java.security.spec.ECGenParameterSpec
import java.security.spec.X509EncodedKeySpec

/**
 * ES256 = ECDSA P-256 + SHA-256 (RDPP/1 §0). Assinatura no formato BRUTO r‖s (64 bytes),
 * base64url sem padding. Chave pública = SPKI DER em base64url.
 * Os providers JCA (Android e JVM) produzem/consomem DER; aqui ficam as conversões DER↔raw.
 */
object Es256 {
    const val RAW_SIGNATURE_LENGTH = 64
    const val JCA_ALGORITHM = "SHA256withECDSA"

    /** Ordem n da curva P-256 (secp256r1). */
    val P256_ORDER = BigInteger("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)

    fun derToRaw(der: ByteArray): ByteArray {
        var p = 0
        fun fail(msg: String): Nothing = throw ProtocolException("SIGNATURE_DER_INVALID", msg)
        fun readLen(): Int {
            if (p >= der.size) fail("truncado")
            val b = der[p++].toInt() and 0xFF
            if (b < 0x80) return b
            if (b == 0x81) {
                if (p >= der.size) fail("truncado")
                val l = der[p++].toInt() and 0xFF
                if (l < 0x80) fail("comprimento não mínimo")
                return l
            }
            fail("comprimento não suportado")
        }
        fun readInt(): ByteArray {
            if (p >= der.size || der[p++].toInt() != 0x02) fail("esperado INTEGER")
            val len = readLen()
            if (len == 0 || p + len > der.size) fail("INTEGER inválido")
            var v = der.copyOfRange(p, p + len)
            p += len
            if (v[0].toInt() and 0x80 != 0) fail("INTEGER negativo")
            // remove zeros à esquerda (o DER usa 0x00 quando o bit alto do valor é 1)
            var start = 0
            while (start < v.size - 1 && v[start].toInt() == 0) start++
            v = v.copyOfRange(start, v.size)
            if (v.size > 32) fail("INTEGER maior que 32 bytes")
            return v
        }
        if (der.isEmpty() || der[p++].toInt() != 0x30) fail("esperado SEQUENCE")
        val seqLen = readLen()
        if (p + seqLen != der.size) fail("comprimento da SEQUENCE incoerente")
        val r = readInt()
        val s = readInt()
        if (p != der.size) fail("bytes extras")
        val out = ByteArray(RAW_SIGNATURE_LENGTH)
        System.arraycopy(r, 0, out, 32 - r.size, r.size)
        System.arraycopy(s, 0, out, 64 - s.size, s.size)
        return out
    }

    fun rawToDer(raw: ByteArray): ByteArray {
        if (raw.size != RAW_SIGNATURE_LENGTH) throw ProtocolException("SIGNATURE_LENGTH", "assinatura bruta deve ter 64 bytes")
        fun enc(part: ByteArray): ByteArray {
            var start = 0
            while (start < part.size - 1 && part[start].toInt() == 0) start++
            val trimmed = part.copyOfRange(start, part.size)
            val needPad = trimmed[0].toInt() and 0x80 != 0
            val body = if (needPad) byteArrayOf(0) + trimmed else trimmed
            return byteArrayOf(0x02, body.size.toByte()) + body
        }
        val r = enc(raw.copyOfRange(0, 32))
        val s = enc(raw.copyOfRange(32, 64))
        val content = r + s
        return byteArrayOf(0x30, content.size.toByte()) + content
    }

    fun publicKeyFromSpki(spki: ByteArray): PublicKey {
        val key = try {
            KeyFactory.getInstance("EC").generatePublic(X509EncodedKeySpec(spki))
        } catch (e: Exception) {
            throw ProtocolException("PUBLIC_KEY_INVALID", "SPKI inválido: ${e.message}")
        }
        val ec = key as? ECPublicKey ?: throw ProtocolException("PUBLIC_KEY_INVALID", "não é chave EC")
        if (ec.params.order != P256_ORDER || ec.params.curve.field.fieldSize != 256) {
            throw ProtocolException("PUBLIC_KEY_INVALID", "curva diferente de P-256")
        }
        return key
    }

    fun publicKeyFromB64u(spkiB64u: String): PublicKey = publicKeyFromSpki(Base64Url.decode(spkiB64u))

    /** Verifica assinatura bruta r‖s. Nunca lança por assinatura malformada: devolve false. */
    fun verify(publicKey: PublicKey, data: ByteArray, rawSignature: ByteArray): Boolean {
        if (rawSignature.size != RAW_SIGNATURE_LENGTH) return false
        val r = BigInteger(1, rawSignature.copyOfRange(0, 32))
        val s = BigInteger(1, rawSignature.copyOfRange(32, 64))
        if (r.signum() == 0 || s.signum() == 0 || r >= P256_ORDER || s >= P256_ORDER) return false
        return try {
            val sig = Signature.getInstance(JCA_ALGORITHM)
            sig.initVerify(publicKey)
            sig.update(data)
            sig.verify(rawToDer(rawSignature))
        } catch (e: Exception) {
            false
        }
    }

    fun verifyB64u(spkiB64u: String, data: ByteArray, signatureB64u: String): Boolean {
        val key = try { publicKeyFromB64u(spkiB64u) } catch (e: ProtocolException) { return false }
        val sig = try { Base64Url.decode(signatureB64u) } catch (e: ProtocolException) { return false }
        return verify(key, data, sig)
    }

    fun signRaw(privateKey: PrivateKey, data: ByteArray): ByteArray {
        val sig = Signature.getInstance(JCA_ALGORITHM)
        sig.initSign(privateKey)
        sig.update(data)
        return derToRaw(sig.sign())
    }

    /** Par de chaves em software (JCA padrão). Uso: testes JVM e verificação cruzada — no aparelho a chave é do Android Keystore. */
    fun generateSoftwareKeyPair(): KeyPair {
        val g = KeyPairGenerator.getInstance("EC")
        g.initialize(ECGenParameterSpec("secp256r1"))
        return g.generateKeyPair()
    }
}

/** Assinador do dispositivo (Android Keystore no aparelho; software em testes). */
interface DeviceSigner {
    /** SPKI DER da chave pública. */
    val publicKeySpki: ByteArray

    /** Assinatura ES256 bruta r‖s (64 bytes). */
    fun sign(data: ByteArray): ByteArray

    fun publicKeyB64u(): String = Base64Url.encode(publicKeySpki)

    fun signB64u(data: ByteArray): String = Base64Url.encode(sign(data))

    fun signRdj1(value: JsonValue): String = signB64u(Rdj1.bytes(value))
}

class SoftwareSigner(private val keyPair: KeyPair = Es256.generateSoftwareKeyPair()) : DeviceSigner {
    override val publicKeySpki: ByteArray get() = keyPair.public.encoded
    override fun sign(data: ByteArray): ByteArray = Es256.signRaw(keyPair.private, data)
}
