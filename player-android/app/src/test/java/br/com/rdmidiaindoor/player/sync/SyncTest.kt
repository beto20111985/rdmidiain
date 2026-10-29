package br.com.rdmidiaindoor.player.sync

import br.com.rdmidiaindoor.player.TestManifests
import br.com.rdmidiaindoor.player.TestManifests.Creative
import br.com.rdmidiaindoor.player.core.ClockService
import br.com.rdmidiaindoor.player.net.ApiClient
import br.com.rdmidiaindoor.player.net.PlayerApi
import br.com.rdmidiaindoor.player.protocol.Hex
import br.com.rdmidiaindoor.player.protocol.Json
import br.com.rdmidiaindoor.player.protocol.JsonValue
import br.com.rdmidiaindoor.player.protocol.ManifestCodec
import br.com.rdmidiaindoor.player.protocol.ServerKey
import br.com.rdmidiaindoor.player.protocol.SoftwareSigner
import br.com.rdmidiaindoor.player.protocol.jsonOf
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import okhttp3.mockwebserver.MockResponse
import okhttp3.mockwebserver.MockWebServer
import okio.Buffer
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.ByteArrayInputStream
import java.io.IOException
import java.io.InputStream
import java.security.MessageDigest
import kotlin.random.Random

class SyncTest {
    @get:Rule
    val tmp = TemporaryFolder()

    private val serverKey = SoftwareSigner()
    private val keys = listOf(ServerKey("srv-1", serverKey.publicKeyB64u(), "ES256"))

    private fun sha(b: ByteArray) = Hex.encode(MessageDigest.getInstance("SHA-256").digest(b))

    /** Fonte fake com Range: pode falhar no meio do corpo (queda de rede) e registra os offsets pedidos. */
    private class FakeSource(val files: Map<String, ByteArray>) : MediaSource {
        val offsets = ArrayList<Pair<String, Long>>()
        var failAfterBytes: Int? = null
        var corrupt: Set<String> = emptySet()
        var calls = 0

        override suspend fun open(mediaId: String, offset: Long, sha256: String): MediaResponse {
            calls++
            offsets += mediaId to offset
            val data = files[mediaId] ?: return MediaResponse(404, null, null)
            val body = if (mediaId in corrupt) data.copyOf().also { it[0] = (it[0] + 1).toByte() } else data
            val slice = body.copyOfRange(offset.toInt(), body.size)
            val limit = failAfterBytes
            val stream: InputStream = if (limit == null) ByteArrayInputStream(slice) else object : InputStream() {
                var pos = 0
                override fun read(): Int {
                    if (pos >= limit) throw IOException("conexão caiu")
                    if (pos >= slice.size) return -1
                    return slice[pos++].toInt() and 0xFF
                }
            }
            failAfterBytes = null
            return MediaResponse(if (offset > 0) 206 else 200, if (offset > 0) offset else null, stream)
        }
    }

    private fun mediaBytes(n: Int, seed: Int) = Random(seed).nextBytes(n)

    /** Manifesto assinado com as mídias reais (sha/size verdadeiros). */
    private fun signedManifest(version: Long, media: Map<String, ByteArray>, extra: (TestManifests.Builder) -> Unit = {}): ByteArray {
        val b = TestManifests.standard22()
        b.version = version
        extra(b)
        val json = b.json()
        val mediaObj = LinkedHashMap<String, JsonValue>()
        for ((k, v) in json.obj("media").fields) {
            val bytes = media[k] ?: error("sem bytes para $k")
            val o = v as JsonValue.Obj
            mediaObj[k] = JsonValue.Obj(LinkedHashMap(o.fields).apply {
                put("sha256", JsonValue.Str(sha(bytes)))
                put("size", JsonValue.Num.of(bytes.size.toLong()))
                put("media_id", JsonValue.Str("mid-$k"))
            })
        }
        val manifest = JsonValue.Obj(LinkedHashMap(json.fields).apply { put("media", JsonValue.Obj(mediaObj)) })
        val env = jsonOf("alg" to "ES256", "key_id" to "srv-1", "signature" to serverKey.signRdj1(manifest), "manifest" to manifest)
        return Json.stringify(env).toByteArray()
    }

    private val allKeys = listOf("i7", "i8", "if1", "ie1", "ve2", "ie3")

    private fun engine(
        store: GenerationStore,
        source: MediaSource,
        envelopes: ArrayDeque<ByteArray?>,
        listener: SyncListener = object : SyncListener {},
        keyList: List<ServerKey> = keys,
    ) = SyncEngine(
        store = store,
        fetcher = { _ -> envelopes.removeFirstOrNull()?.let { ManifestFetch.Body(it) } ?: ManifestFetch.NotModified },
        downloader = MediaDownloader(store, source, bufferSize = 7),
        serverKeys = { keyList },
        expectedScreenId = { "screen-1" },
        listener = listener,
        nowMs = { 1_000L },
        monoMs = { 0L },
        sleep = { },
    )

    @Test
    fun resumableDownload_rangeFromPartialOffset_incrementalSha() = runTest {
        val store = GenerationStore(tmp.newFolder("rd"))
        val data = mediaBytes(1_000, 1)
        val src = FakeSource(mapOf("m" to data)).apply { failAfterBytes = 400 }
        val dl = MediaDownloader(store, src, bufferSize = 64)
        val ref = br.com.rdmidiaindoor.player.protocol.Manifest.MediaRef("k", "m", sha(data), data.size.toLong(), "image/jpeg", null, null, null)
        val first = dl.download(ref)
        assertTrue(first is DownloadOutcome.Failed)
        assertTrue(store.partFile(ref.sha256).length() in 1..400) // parcial mantido para retomar
        val partial = store.partFile(ref.sha256).length()
        val second = dl.download(ref)
        assertTrue(second is DownloadOutcome.Verified)
        assertEquals("m" to partial, src.offsets.last()) // retomou do offset (Range)
        assertArrayEquals(data, store.mediaFile(ref.sha256).readBytes())
        assertFalse(store.partFile(ref.sha256).exists())
    }

    @Test
    fun shaMismatch_isNeverActivated_permanentFailureAfter5Attempts_versionActivatesWithoutIt() = runTest {
        val store = GenerationStore(tmp.newFolder("rd"))
        val media = allKeys.associateWith { mediaBytes(300 + it.length, it.hashCode()) }
        val src = FakeSource(media.mapKeys { "mid-${it.key}" }).apply { corrupt = setOf("mid-ie1") }
        val errors = ArrayList<Triple<String, String, Int>>()
        val listener = object : SyncListener {
            override fun onMediaError(mediaId: String, reason: String, attempts: Int) { errors += Triple(mediaId, reason, attempts) }
        }
        val out = engine(store, src, ArrayDeque(listOf(signedManifest(1, media))), listener).sync()
        assertTrue(out is SyncOutcome.Staged)
        val st = store.loadState(1)!!
        assertFalse("ie1" in st.readyKeys())
        assertEquals(setOf("i7", "i8", "if1", "ve2", "ie3"), st.readyMediaKeys)
        assertEquals(listOf(Triple("mid-ie1", "SHA256_MISMATCH", 5)), errors)
        assertFalse(store.mediaFile(sha(media.getValue("ie1"))).exists()) // nunca promovida
        assertEquals(1L, store.commitStaged())
        assertEquals(1L, store.pointer().active)
    }

    private fun GenerationState.readyKeys() = readyMediaKeys

    @Test
    fun invalidSignature_orWrongScreen_orStaleVersion_keepsActiveVersion() = runTest {
        val store = GenerationStore(tmp.newFolder("rd"))
        val media = allKeys.associateWith { mediaBytes(200, it.hashCode()) }
        val src = FakeSource(media.mapKeys { "mid-${it.key}" })
        val failures = ArrayList<String>()
        val listener = object : SyncListener {
            override fun onManifestFailed(version: Long?, reason: String) { failures += reason }
        }
        // v5 válido ⇒ ativo
        assertTrue(engine(store, src, ArrayDeque(listOf(signedManifest(5, media)))).sync() is SyncOutcome.Staged)
        store.commitStaged()
        // assinatura de outra chave
        val wrongKey = listOf(ServerKey("srv-1", SoftwareSigner().publicKeyB64u(), "ES256"))
        assertTrue(engine(store, src, ArrayDeque(listOf(signedManifest(6, media))), listener, wrongKey).sync() is SyncOutcome.Failed)
        // tela diferente
        assertTrue(engine(store, src, ArrayDeque(listOf(patchScreen(signedManifest(7, media)))), listener).sync() is SyncOutcome.Failed)
        // versão antiga (rollback de manifesto)
        assertTrue(engine(store, src, ArrayDeque(listOf(signedManifest(4, media))), listener).sync() is SyncOutcome.Failed)
        assertEquals(listOf("SIGNATURE_INVALID", "SCREEN_MISMATCH", "STALE_VERSION"), failures)
        assertEquals(5L, store.pointer().active)
        assertNull(store.pointer().staged)
    }

    /** Re-assina o manifesto com outro screen_id. */
    private fun patchScreen(envelope: ByteArray): ByteArray {
        val env = Json.parseObject(envelope)
        val m = JsonValue.Obj(LinkedHashMap(env.obj("manifest").fields).apply { put("screen_id", JsonValue.Str("outra-tela")) })
        return Json.stringify(jsonOf("alg" to "ES256", "key_id" to "srv-1", "signature" to serverKey.signRdj1(m), "manifest" to m)).toByteArray()
    }

    @Test
    fun atomicCommit_rollback_andGc() = runTest {
        val store = GenerationStore(tmp.newFolder("rd"))
        val mediaV1 = allKeys.associateWith { mediaBytes(150, it.hashCode()) }
        val mediaV2 = mediaV1 + ("ie1" to mediaBytes(170, 99))
        val src = FakeSource(mediaV1.mapKeys { "mid-${it.key}" })
        engine(store, src, ArrayDeque(listOf(signedManifest(1, mediaV1)))).sync()
        assertEquals(1L, store.commitStaged())
        val src2 = FakeSource(mediaV2.mapKeys { "mid-${it.key}" })
        val e2 = engine(store, src2, ArrayDeque(listOf(signedManifest(2, mediaV2))))
        assertTrue(e2.sync() is SyncOutcome.Staged)
        // staged não altera a versão ativa até o COMMIT (fronteira de ciclo)
        assertEquals(Pointer(active = 1, previous = null, staged = 2), store.pointer())
        assertEquals(2L, store.commitStaged())
        assertEquals(Pointer(active = 2, previous = 1, staged = null), store.pointer())
        // rollback ⇒ versão anterior volta a ser a ativa (arquivos preservados)
        assertEquals(1L, store.rollback())
        assertEquals(1L, store.pointer().active)
        assertTrue(store.loadEnvelope(1) != null && store.loadEnvelope(2) != null)
        // GC com ativa=1: a mídia exclusiva da v2 (fora de {ativa, staged, anterior}) é removida
        val oldIe1 = sha(mediaV1.getValue("ie1"))
        val newIe1 = sha(mediaV2.getValue("ie1"))
        assertTrue(store.mediaFile(newIe1).exists())
        val report = store.gc(1_000L, { v -> store.loadEnvelope(v)?.let { ManifestCodec.parse(ManifestCodec.parseEnvelope(it).manifestJson) } })
        assertFalse(store.mediaFile(newIe1).exists())
        assertTrue(store.mediaFile(oldIe1).exists())
        assertEquals(1, report.removedMedia)
        assertFalse(store.genPath(2).exists())
    }

    @Test
    fun gc_removesExpiredButKeepsFallback() {
        val store = GenerationStore(tmp.newFolder("rd"))
        val b = TestManifests.standard22()
        b.creatives["e1"] = Creative("e1", "EXTERNAL", "ie1", validUntil = 500L)
        val m = b.build()
        m.media.values.forEach { store.mediaFile(it.sha256).writeBytes(ByteArray(it.size.toInt())) }
        store.stage(42, "{}".toByteArray(), m.media.keys, emptyList(), 0)
        store.commitStaged()
        store.gc(1_000L, { m })
        assertFalse(store.mediaFile(m.media.getValue("ie1").sha256).exists()) // vencida
        assertTrue(store.mediaFile(m.media.getValue("if1").sha256).exists()) // fallback
        assertTrue(store.mediaFile(m.media.getValue("ie3").sha256).exists())
    }

    @Test
    fun httpClient_sendsQuotedIfNoneMatch_andRangeIfRange() = runTest {
        val server = MockWebServer()
        server.enqueue(MockResponse().setResponseCode(304).addHeader("X-Server-Time-Ms", "5000"))
        server.enqueue(MockResponse().setResponseCode(206).addHeader("Content-Range", "bytes 10-19/20").setBody(Buffer().write(ByteArray(10) { 7 })))
        server.start()
        try {
            val clock = ClockService({ 1_000L }, { 0L })
            val client = ApiClient(OkHttpClient(), { server.url("/").toString() }, clock)
            val api = PlayerApi(client)
            assertTrue(api.manifest("tok", 42) is ManifestFetch.NotModified)
            val r1 = server.takeRequest()
            assertEquals("\"42\"", r1.getHeader("If-None-Match"))
            assertEquals("Bearer tok", r1.getHeader("Authorization"))
            assertEquals(4_000L, clock.offsetMs)
            val sha = "c".repeat(64)
            val media = client.openMedia("m-1", 10, sha, "tok")
            assertEquals(206, media.code)
            assertEquals(10L, media.rangeStart)
            media.close()
            val r2 = server.takeRequest()
            assertEquals("/api/v1/player/media/m-1", r2.path)
            assertEquals("bytes=10-", r2.getHeader("Range"))
            assertEquals("\"$sha\"", r2.getHeader("If-Range"))
        } finally {
            server.shutdown()
        }
    }
}
