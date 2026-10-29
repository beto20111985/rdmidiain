package br.com.rdmidiaindoor.player.protocol

import br.com.rdmidiaindoor.player.TestManifests
import br.com.rdmidiaindoor.player.config.PinHasher
import br.com.rdmidiaindoor.player.net.ApiClient
import br.com.rdmidiaindoor.player.net.Backoff
import br.com.rdmidiaindoor.player.net.UrlPolicy
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import javax.crypto.SecretKeyFactory
import javax.crypto.spec.PBEKeySpec
import kotlin.random.Random

class ManifestAndMessagesTest {

    private fun expectManifestError(code: String, json: JsonValue.Obj) {
        try {
            ManifestCodec.parse(json)
            fail("esperado $code")
        } catch (e: ProtocolException) {
            assertEquals(code, e.code)
        }
    }

    private fun JsonValue.Obj.with(key: String, v: Any?) = JsonValue.Obj(LinkedHashMap(fields).apply { put(key, Json.from(v)) })

    /** Exemplo literal do RDPP/1 §5 (com referências coerentes). */
    private val specExample = """
        {"schema":"rd.manifest/1","manifest_id":"5b3c3b2e-0000-4000-8000-000000000001","version":42,
         "screen_id":"5b3c3b2e-0000-4000-8000-000000000002","screen_code":"TEL-GO-7Q3M","generated_at_ms":0,"timezone":"America/Manaus",
         "rotation":{"internal_ms":120000,"external_ms":120000,"cycle_ms":240000,"unit_ms":30000,"policy_id":"p"},
         "anchor_ms":1791380000000,
         "horizon":{"from_ms":0,"to_ms":0,"repeat_period_ms":604800000},
         "settings":{"image_default_ms":30000,"fade_ms":400,"orientation":"LANDSCAPE","footer":{"enabled":true,"height_permille":60},
                     "offline_alert_days":[7,28],"heartbeat_interval_ms":30000,"telemetry_interval_ms":300000,"panel_url":"https://painel.test"},
         "media":{"m1":{"media_id":"m-1","sha256":"${"a".repeat(64)}","size":1234,"mime":"video/mp4","width":1920,"height":1080,"duration_ms":30000}},
         "creatives":{"c1":{"creative_id":"cr-1","code":"RD-A7M2P9","kind":"EXTERNAL","campaign_id":"ca-1","advertiser_id":"ad-1",
                            "media":"m1","duration_ms":30000,"valid_from_ms":0,"valid_until_ms":1791999999999,"label":"Publicidade"}},
         "internal_pattern":[[{"c":"c1","d":30000}]],
         "fallback":[{"c":"c1","d":30000}],
         "external_plan":{"1791381600000":[{"i":"ins-1","c":"c1","u":0,"d":30000}]},
         "emergency":[{"id":"em-1","c":"c1","from_ms":0,"until_ms":0}],
         "revoked":["cr-9"]}
    """.trimIndent()

    @Test
    fun parsesSpecExample() {
        val m = ManifestCodec.parse(specExample.toByteArray())
        assertEquals(42L, m.version)
        assertEquals(1_791_380_000_000L, m.anchorMs)
        assertEquals(240_000L, m.rotation.cycleMs)
        assertEquals(listOf(7, 28), m.settings.offlineAlertDays)
        assertEquals(60, m.settings.footer.heightPermille)
        assertTrue(m.media.getValue("m1").isVideo)
        assertEquals("Publicidade", m.creatives.getValue("c1").label)
        assertEquals(listOf(Manifest.ExternalEntry("ins-1", "c1", 0, 30_000)), m.externalPlan[1_791_381_600_000L])
        assertEquals(setOf("cr-9"), m.revoked)
        assertNull(ManifestCodec.parse(TestManifests.Builder(null).json()).anchorMs)
    }

    @Test
    fun rejectsStructuralViolations() {
        val base = TestManifests.standard22().json()
        expectManifestError("MANIFEST_SCHEMA", base.with("schema", "rd.manifest/2"))
        expectManifestError("MISSING_FIELD", JsonValue.Obj(LinkedHashMap(base.fields).apply { remove("fallback") }))
        expectManifestError(
            "MANIFEST_INVALID",
            base.with("rotation", jsonOf("internal_ms" to 120_000, "external_ms" to 150_000, "cycle_ms" to 270_000, "unit_ms" to 30_000, "policy_id" to "p")),
        )
        expectManifestError(
            "MANIFEST_INVALID",
            base.with("rotation", jsonOf("internal_ms" to 120_000, "external_ms" to 120_000, "cycle_ms" to 200_000, "unit_ms" to 30_000, "policy_id" to "p")),
        )
        expectManifestError("MANIFEST_INVALID", base.with("fallback", listOf(jsonOf("c" to "nao-existe", "d" to 30_000))))
        expectManifestError("FLOAT_NOT_ALLOWED", base.with("generated_at_ms", JsonValue.Num("1.5", null)))
    }

    @Test
    fun wsEnvelope_andMessageShapes() {
        val env = WsEnvelope.parse("""{"type":"command","id":"x","ts_ms":5,"data":{"command_id":"c1","command":"resync","args":{},"expires_at_ms":99}}""")
        val cmd = CommandMessage.from(env.data)
        assertEquals("resync", cmd.command)
        assertEquals(99L, cmd.expiresAtMs)
        val hb = PlayerMessages.heartbeat(
            "PLAYING", PlayerMessages.Current("RD-A7M2P9", 10), 42, null, null, 1, 2,
            PlayerMessages.Storage(100, 50, true), 0, listOf("OFFLINE_7D"),
        )
        assertEquals(
            setOf("state", "current", "manifest_version_applied", "manifest_version_pending", "download", "last_sync_ms", "last_play_ms", "storage", "clock_offset_ms", "alerts"),
            hb.fields.keys,
        )
        val tel = PlayerMessages.telemetry(
            PlayerMessages.Telemetry(5, 900, 2000, 1000, 8000, 3600, "Android 11", "1.0.0", "android", "1920x1080", "LANDSCAPE", "Loja", -60, true),
        )
        assertFalse(Json.stringify(tel).contains("\"ip"))
        assertEquals("Loja", tel.obj("wifi").str("ssid"))
        val noWifi = PlayerMessages.telemetry(
            PlayerMessages.Telemetry(null, 900, 2000, 1000, 8000, 3600, "Android 11", "1.0.0", "android", "1920x1080", "LANDSCAPE", null, null, null),
        )
        assertEquals(JsonValue.Null, noWifi["wifi"])
        val encoded = WsEnvelope.of("heartbeat", hb, 7, "id-1").encode()
        assertEquals("heartbeat", WsEnvelope.parse(encoded).type)
    }

    @Test
    fun urlPolicy_httpsByDefault_httpOnlyForConfiguredDevHosts() {
        val dev = setOf("localhost", "10.0.2.2")
        assertNull(UrlPolicy.validate("https://api.exemplo.com.br", dev))
        assertNull(UrlPolicy.validate("http://10.0.2.2:8000/", dev))
        assertTrue(UrlPolicy.validate("http://api.exemplo.com.br", dev) != null)
        assertTrue(UrlPolicy.validate("https://api.exemplo.com.br/?token=x", dev) != null)
        assertTrue(UrlPolicy.validate("ftp://x", dev) != null)
        assertEquals("wss://api.test/ws/player?ticket=abc", UrlPolicy.wsUrl("https://api.test/", "/ws/player", mapOf("ticket" to "abc")))
        assertEquals("ws://localhost:8000/rd/ws/player/pairing?request_id=r1", UrlPolicy.wsUrl("http://localhost:8000/rd", "/ws/player/pairing", mapOf("request_id" to "r1")))
        assertEquals(1024L, ApiClient.parseContentRangeStart("bytes 1024-2047/4096"))
    }

    @Test
    fun backoff_exponentialWithJitter_boundedAndResettable() {
        val b = Backoff(1_000, 60_000, random = Random(3))
        val values = (0 until 12).map { b.next() }
        values.forEach { assertTrue(it in 1_000..60_000) }
        assertTrue(values[0] <= 1_000)
        assertTrue(values.last() >= 30_000)
        b.reset()
        assertEquals(0, b.attempt)
        assertTrue(b.next() <= 1_000)
        val pairing = Backoff(5_000, 60_000, random = Random(9))
        repeat(20) { assertTrue(pairing.next() >= 5_000) } // pareamento: ≥ 5 s
    }

    @Test
    fun pinHasher_pbkdf2MatchesJdk_andVerifies() {
        val salt = ByteArray(16) { it.toByte() }
        val mine = PinHasher.pbkdf2("1234".toByteArray(), salt, 1_000, 32)
        val jdk = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256").generateSecret(PBEKeySpec("1234".toCharArray(), salt, 1_000, 256)).encoded
        assertTrue(mine.contentEquals(jdk))
        val h = PinHasher.hash("2468", iterations = 2_000)
        assertTrue(h.startsWith("pbkdf2-sha256$2000$"))
        assertTrue(PinHasher.verify("2468", h))
        assertFalse(PinHasher.verify("2469", h))
        assertTrue(PinHasher.isValidEncoded(h))
        assertFalse(PinHasher.isValidEncoded("plain-1234"))
        assertFalse(PinHasher.isAcceptablePin("12a456"))
        assertFalse(PinHasher.isAcceptablePin("12345")) // backend: 6 a 12 dígitos
        assertTrue(PinHasher.isAcceptablePin("000000"))
        assertEquals(210_000, PinHasher.DEFAULT_ITERATIONS)
    }

    /** Formato normativo do comando `set_tech_pin` (RDPP §4), igual ao `tech_pin_args` do backend. */
    @Test
    fun setTechPinArgs_normativeFormat() {
        val salt = ByteArray(16) { (it * 7).toByte() }
        val iterations = 210_000
        val jdk = SecretKeyFactory.getInstance("PBKDF2WithHmacSHA256")
            .generateSecret(PBEKeySpec("246813".toCharArray(), salt, iterations, 256)).encoded
        val stored = PinHasher.fromCommandArgs("pbkdf2-sha256", iterations.toLong(), Base64Url.encode(salt), Base64Url.encode(jdk))!!
        assertTrue(PinHasher.verify("246813", stored))
        assertFalse(PinHasher.verify("246814", stored))
        assertNull(PinHasher.fromCommandArgs("bcrypt", 210_000, Base64Url.encode(salt), Base64Url.encode(jdk)))
        assertNull(PinHasher.fromCommandArgs("pbkdf2-sha256", 1_000, Base64Url.encode(salt), Base64Url.encode(jdk)))
        assertNull(PinHasher.fromCommandArgs("pbkdf2-sha256", 210_000, Base64Url.encode(salt.copyOf(8)), Base64Url.encode(jdk)))
        assertNull(PinHasher.fromCommandArgs("pbkdf2-sha256", 210_000, Base64Url.encode(salt), Base64Url.encode(jdk.copyOf(16))))
        assertNull(PinHasher.fromCommandArgs("pbkdf2-sha256", 210_000, Base64Url.encode(salt) + "=", Base64Url.encode(jdk)))
        assertTrue(Commands.SET_TECH_PIN in Commands.STANDARD)
    }
}
