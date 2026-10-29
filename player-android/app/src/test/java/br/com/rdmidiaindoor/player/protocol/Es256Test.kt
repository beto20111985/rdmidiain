package br.com.rdmidiaindoor.player.protocol

import br.com.rdmidiaindoor.player.TestManifests
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.security.KeyPairGenerator
import java.security.Signature
import java.security.spec.ECGenParameterSpec

/** ES256 com provider JVM (SunEC): DER↔raw r‖s, verificação, SPKI P-256, envelope do manifesto, oferta de OTA. */
class Es256Test {
    private val kp = Es256.generateSoftwareKeyPair()
    private val signer = SoftwareSigner(kp)

    @Test
    fun derRawRoundTrip_manySignatures_includingShortAndHighBitIntegers() {
        var sawPadded = false
        var sawShort = false
        repeat(300) { i ->
            val data = "msg-$i".toByteArray()
            val der = Signature.getInstance("SHA256withECDSA").run { initSign(kp.private); update(data); sign() }
            val raw = Es256.derToRaw(der)
            assertEquals(64, raw.size)
            assertArrayEquals(der, Es256.rawToDer(raw))
            if (der[3].toInt() == 33) sawPadded = true
            if (der[3].toInt() < 32) sawShort = true
            assertTrue(Es256.verify(kp.public, data, raw))
        }
        assertTrue("deve haver r com bit alto (0x00 no DER)", sawPadded)
        // r curto (< 32 bytes) é raro (~1/256); validado explicitamente abaixo
        val r = ByteArray(32).also { it[31] = 5 }
        val s = ByteArray(32) { 0x7f }
        val raw = r + s
        val der = Es256.rawToDer(raw)
        assertArrayEquals(byteArrayOf(0x30, 0x25, 0x02, 0x01, 0x05, 0x02, 0x20) + s, der)
        assertArrayEquals(raw, Es256.derToRaw(der))
        println("r curto observado nas assinaturas aleatórias: $sawShort")
    }

    @Test
    fun verify_rejectsTamperedDataSignatureAndInvalidScalars() {
        val data = Rdj1.bytes(DeviceAuthPayloads.auth("ch-1", "dev-1"))
        val sig = signer.sign(data)
        assertTrue(Es256.verify(kp.public, data, sig))
        assertFalse(Es256.verify(kp.public, data + 1, sig))
        assertFalse(Es256.verify(kp.public, data, sig.copyOf().also { it[10] = (it[10] + 1).toByte() }))
        assertFalse(Es256.verify(kp.public, data, sig.copyOf(63)))
        assertFalse(Es256.verify(kp.public, data, ByteArray(64)))
        val sGeN = ByteArray(32) + Es256.P256_ORDER.toByteArray().takeLast(32).toByteArray()
        assertFalse(Es256.verify(kp.public, data, sig.copyOfRange(0, 32) + sGeN.copyOfRange(32, 64)))
        assertTrue(Es256.verifyB64u(signer.publicKeyB64u(), data, signer.signB64u(data)))
        assertFalse(Es256.verifyB64u("lixo", data, signer.signB64u(data)))
    }

    @Test
    fun spki_onlyP256Accepted() {
        assertEquals(kp.public, Es256.publicKeyFromSpki(kp.public.encoded))
        val p384 = KeyPairGenerator.getInstance("EC").apply { initialize(ECGenParameterSpec("secp384r1")) }.generateKeyPair()
        try {
            Es256.publicKeyFromSpki(p384.public.encoded)
            fail("P-384 deve ser rejeitada")
        } catch (e: ProtocolException) {
            assertEquals("PUBLIC_KEY_INVALID", e.code)
        }
    }

    @Test
    fun manifestEnvelope_signatureOverRdj1_tamperAndKeyIdChecks() {
        val manifestJson = TestManifests.standard22().json()
        val env = ManifestEnvelope("ES256", "srv-1", signer.signRdj1(manifestJson), manifestJson)
        val keys = listOf(ServerKey("srv-1", signer.publicKeyB64u(), "ES256"))
        assertTrue(ManifestCodec.verify(env, keys))
        // envelope serializado/re-parseado (ordem de chaves diferente) continua válido: assinatura é sobre RDJ1
        val wire = Json.stringify(jsonOf("alg" to "ES256", "key_id" to "srv-1", "signature" to env.signature, "manifest" to manifestJson))
        assertTrue(ManifestCodec.verify(ManifestCodec.parseEnvelope(wire.toByteArray()), keys))
        val tampered = JsonValue.Obj(LinkedHashMap(manifestJson.fields).apply { put("version", JsonValue.Num.of(43)) })
        assertFalse(ManifestCodec.verify(env.copy(manifestJson = tampered), keys))
        assertFalse(ManifestCodec.verify(env.copy(keyId = "srv-2"), keys))
        assertFalse(ManifestCodec.verify(env.copy(alg = "HS256"), keys))
        assertFalse(ManifestCodec.verify(env, listOf(ServerKey("srv-1", SoftwareSigner().publicKeyB64u(), "ES256"))))
    }

    @Test
    fun appUpdateOffer_releaseKeySignature_urlOutsideSignature() {
        val release = SoftwareSigner()
        val unsigned = AppUpdateOffer("1.2.0", "https://cdn.exemplo.test/rd.apk", "ab".repeat(32), 12_345, "", "rel-1", false)
        val sig = release.signRdj1(unsigned.signedPayload("android"))
        val offer = unsigned.copy(signature = sig)
        val keys = mapOf("rel-1" to release.publicKeyB64u())
        assertTrue(offer.verify("android", keys))
        assertTrue(offer.copy(url = "https://outra.cdn.test/x.apk").verify("android", keys)) // url fora da assinatura
        assertFalse(offer.copy(size = 12_346).verify("android", keys))
        assertFalse(offer.copy(version = "1.2.1").verify("android", keys))
        assertFalse(offer.verify("windows", keys))
        assertFalse(offer.copy(keyId = "rel-2").verify("android", keys))
        assertFalse(offer.verify("android", emptyMap()))
        assertEquals(
            """{"platform":"android","purpose":"rd-app-update-v1","sha256":"${"ab".repeat(32)}","size":12345,"version":"1.2.0"}""",
            Rdj1.encode(unsigned.signedPayload("android")),
        )
        assertEquals(mapOf("a" to "X", "b" to "Y"), AppUpdateOffer.parseKeys(" a=X ; b=Y;;"))
    }

    @Test
    fun devicePayloads_canonicalForms() {
        assertEquals("""{"challenge":"c","device_id":"d","purpose":"rd-player-auth-v1"}""", Rdj1.encode(DeviceAuthPayloads.auth("c", "d")))
        assertEquals(
            """{"pairing_request_id":"p","purpose":"rd-pairing-result-v1","ts_ms":1791380000000}""",
            Rdj1.encode(DeviceAuthPayloads.pairingResult("p", 1_791_380_000_000L)),
        )
    }
}
