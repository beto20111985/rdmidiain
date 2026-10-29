package br.com.rdmidiaindoor.player.pop

import br.com.rdmidiaindoor.player.protocol.Base64Url
import br.com.rdmidiaindoor.player.protocol.Es256
import br.com.rdmidiaindoor.player.protocol.Hex
import br.com.rdmidiaindoor.player.protocol.Json
import br.com.rdmidiaindoor.player.protocol.JsonValue
import br.com.rdmidiaindoor.player.protocol.Rdj1
import br.com.rdmidiaindoor.player.protocol.SoftwareSigner
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class PopTest {
    private val signer = SoftwareSigner()

    private fun input(result: PopResult = PopResult.COMPLETED, actual: Long = 30_000, start: Long = 1_791_380_000_000L) = PopInput(
        creativeId = "cr-1", creativeCode = "RD-A7M2P9", campaignId = "camp-1", insertionId = "ins-1", kind = "EXTERNAL",
        manifestVersion = 42, startedAtMs = start, expectedMs = 30_000, actualMs = actual, result = result,
        clockOffsetMs = 120, offline = true,
    )

    @Test
    fun completedOnlyWhenFinishedIntegrally_noTolerance() {
        assertEquals(PopResult.COMPLETED, PopRules.result(30_000, 30_000, endedNaturally = true, interrupted = false, error = false))
        assertEquals(PopResult.COMPLETED, PopRules.result(30_000, 30_412, endedNaturally = true, interrupted = false, error = false))
        assertEquals(PopResult.INTERRUPTED, PopRules.result(30_000, 29_999, endedNaturally = true, interrupted = false, error = false))
        assertEquals(PopResult.INTERRUPTED, PopRules.result(30_000, 25_000, endedNaturally = true, interrupted = false, error = false))
        assertEquals(PopResult.INTERRUPTED, PopRules.result(30_000, 31_000, endedNaturally = false, interrupted = false, error = false))
        assertEquals(PopResult.INTERRUPTED, PopRules.result(30_000, 31_000, endedNaturally = true, interrupted = true, error = false))
        assertEquals(PopResult.ERROR, PopRules.result(30_000, 31_000, endedNaturally = true, interrupted = false, error = true))
    }

    @Test
    fun eventHasAllKeys_signedOverRdj1_chainedByPrevHash_seqMonotonic() {
        val store = InMemoryPopStore()
        val rec = PopRecorder(store, signer)
        val r1 = rec.record("dev-1", "scr-1", input())
        val r2 = rec.record("dev-1", "scr-1", input(PopResult.INTERRUPTED, 12_000))
        val e1 = Json.parseObject(r1.eventJson)
        val e2 = Json.parseObject(r2.eventJson)
        val keys = setOf(
            "schema", "playback_uuid", "device_id", "screen_id", "creative_id", "creative_code", "campaign_id", "insertion_id", "kind",
            "manifest_version", "started_at_ms", "finished_at_ms", "expected_ms", "actual_ms", "result", "seq", "nonce", "prev_hash",
            "clock_offset_ms", "offline",
        )
        assertEquals(keys, e1.fields.keys)
        assertEquals("rd.pop/1", e1.str("schema"))
        assertEquals(1L, e1.long("seq"))
        assertEquals(2L, e2.long("seq"))
        assertEquals("0".repeat(64), e1.str("prev_hash"))
        assertEquals(Hex.encode(Rdj1.sha256(r1.eventJson.toByteArray())), e2.str("prev_hash"))
        // texto gravado já é RDJ1 canônico
        assertEquals(r1.eventJson, Rdj1.encode(e1))
        // finished = started + actual (monotônico); tempos corrigidos
        assertEquals(e1.long("started_at_ms") + 30_000, e1.long("finished_at_ms"))
        // assinatura ES256 bruta (64 bytes) verificável com a chave pública do dispositivo
        val sig = Base64Url.decode(r1.signature)
        assertEquals(64, sig.size)
        assertTrue(Es256.verify(Es256.publicKeyFromSpki(signer.publicKeySpki), r1.eventJson.toByteArray(), sig))
        // nonce de 16 bytes, inédito
        assertEquals(16, Base64Url.decode(e1.str("nonce")).size)
        assertNotEquals(e1.str("nonce"), e2.str("nonce"))
        assertNotEquals(e1.str("playback_uuid"), e2.str("playback_uuid"))
        assertEquals(JsonValue.Bool(true), e1["offline"])
    }

    @Test
    fun seqPersistsAcrossRestart_neverResets() {
        val persisted = InMemoryPopStore() // simula o banco (Room) que sobrevive ao reboot
        PopRecorder(persisted, signer).apply { repeat(3) { record("dev-1", "scr-1", input()) } }
        val afterReboot = PopRecorder(persisted, signer) // nova instância (novo processo)
        val r = afterReboot.record("dev-1", "scr-1", input())
        assertEquals(4L, r.seq)
        assertEquals(4L, persisted.chainState("dev-1").lastSeq)
        // outro device_id (re-pareamento) tem cadeia própria
        assertEquals(1L, afterReboot.record("dev-2", "scr-1", input()).seq)
    }

    @Test
    fun skippedEvent_shape() {
        val rec = PopRecorder(InMemoryPopStore(), signer)
        val now = 1_791_380_010_000L
        val r = rec.record("dev-1", "scr-1", input(PopResult.SKIPPED, 0, now))
        val e = Json.parseObject(r.eventJson)
        assertEquals("SKIPPED", e.str("result"))
        assertEquals(now, e.long("started_at_ms"))
        assertEquals(now, e.long("finished_at_ms"))
        assertEquals(0L, e.long("actual_ms"))
        assertEquals(30_000L, e.long("expected_ms"))
    }

    @Test
    fun uploader_removesOnlyDefinitivelyAcknowledged() = runTest {
        val store = InMemoryPopStore()
        val rec = PopRecorder(store, signer)
        val rows = (1..3).map { rec.record("dev-1", "scr-1", input()) }
        var body = ""
        val up = PopUploader(store, { b ->
            body = b
            """{"results":[{"playback_uuid":"${rows[0].playbackUuid}","status":"ACCEPTED","valid":true,"reason":null},""" +
                """{"playback_uuid":"${rows[1].playbackUuid}","status":"DUPLICATE","valid":false,"reason":"DUPLICATE"}],"ack_upto_seq":2}"""
        })
        val res = up.flush(maxBatches = 1)
        assertEquals(3, res.sent)
        assertEquals(2, res.removed)
        assertEquals(2L, res.lastAckSeq)
        assertEquals(listOf(rows[2].playbackUuid), store.pending(10).map { it.playbackUuid })
        // corpo do lote: {"events":[{"event":{…},"signature":"…"}]}
        val parsed = Json.parseObject(body)
        val ev = parsed.arr("events")
        assertEquals(3, ev.size)
        val first = ev[0] as JsonValue.Obj
        assertEquals(rows[0].eventJson, Rdj1.encode(first.obj("event")))
        assertEquals(rows[0].signature, first.str("signature"))
    }

    @Test
    fun uploader_batchesOf200() = runTest {
        val store = InMemoryPopStore()
        val rec = PopRecorder(store, signer)
        repeat(450) { rec.record("dev-1", "scr-1", input()) }
        val sizes = ArrayList<Int>()
        val up = PopUploader(store, { b ->
            val evs = Json.parseObject(b).arr("events")
            sizes += evs.size
            val results = evs.joinToString(",") { e ->
                val id = (e as JsonValue.Obj).obj("event").str("playback_uuid")
                """{"playback_uuid":"$id","status":"ACCEPTED","valid":true,"reason":null}"""
            }
            """{"results":[$results],"ack_upto_seq":0}"""
        })
        up.flush()
        assertEquals(listOf(200, 200, 50), sizes)
        assertEquals(0, store.count())
    }
}
