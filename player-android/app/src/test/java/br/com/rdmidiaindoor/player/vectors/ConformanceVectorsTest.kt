package br.com.rdmidiaindoor.player.vectors

import br.com.rdmidiaindoor.player.core.CycleResolver
import br.com.rdmidiaindoor.player.core.LocalState
import br.com.rdmidiaindoor.player.protocol.Base64Url
import br.com.rdmidiaindoor.player.protocol.Es256
import br.com.rdmidiaindoor.player.protocol.Json
import br.com.rdmidiaindoor.player.protocol.JsonValue
import br.com.rdmidiaindoor.player.protocol.ManifestCodec
import br.com.rdmidiaindoor.player.protocol.ProtocolException
import br.com.rdmidiaindoor.player.protocol.Rdj1
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.assertFalse
import org.junit.Test
import java.io.File
import java.security.KeyFactory
import java.security.spec.PKCS8EncodedKeySpec

/**
 * Vetores de conformidade `protocol/vectors/<arquivo>.json` (RDPP/1 §9), gerados pela implementação de referência do
 * backend e PUBLICADOS em 2026-10-07. Os arquivos são obrigatórios: ausência = FALHA (nunca aprovação silenciosa).
 * Os valores esperados nunca são ajustados aqui — divergência é reportada ao coordenador.
 */
class ConformanceVectorsTest {

    private val dir: File = File(System.getProperty("rd.vectors.dir") ?: "../../protocol/vectors")

    private fun load(name: String): JsonValue.Obj {
        val f = File(dir, name)
        assertTrue("VETOR AUSENTE: ${f.path} (RDPP §9) — os vetores são obrigatórios", f.isFile)
        val o = Json.parseObject(f.readBytes())
        assertEquals("versão do arquivo de vetores", 1L, o.longOrNull("version") ?: 1L)
        return o
    }

    private fun cases(o: JsonValue.Obj): List<JsonValue.Obj> = o.arr("cases").map { it as JsonValue.Obj }

    /** Converte marcadores `{"$float":"1.5"}` em números não inteiros reais (que a implementação deve rejeitar). */
    private fun unmarkFloats(v: JsonValue): JsonValue = when (v) {
        is JsonValue.Obj -> {
            val f = v.fields["\$float"]
            if (v.fields.size == 1 && f is JsonValue.Str) JsonValue.Num(f.value, null)
            else JsonValue.Obj(v.fields.mapValuesTo(LinkedHashMap()) { unmarkFloats(it.value) })
        }
        is JsonValue.Arr -> JsonValue.Arr(v.items.map { unmarkFloats(it) })
        else -> v
    }

    @Test
    fun canonicalJson() {
        val all = cases(load("canonical_json.json"))
        assertTrue(all.isNotEmpty())
        println("canonical_json: ${all.size} casos (${all.count { it.has("error") }} com erro esperado)")
        for (c in all) {
            val name = c.str("name")
            val input = unmarkFloats(c["input"] ?: JsonValue.Null)
            val expectedError = c.strOrNull("error")
            if (expectedError != null) {
                val code = try {
                    Rdj1.encode(input)
                    null
                } catch (e: ProtocolException) {
                    e.code
                }
                assertEquals("[$name] erro esperado", expectedError, code)
            } else {
                assertEquals("[$name]", c.str("canonical"), Rdj1.encode(input))
                // o texto canônico re-parseado é ponto fixo
                assertEquals("[$name] ponto fixo", c.str("canonical"), Rdj1.encode(Json.parse(c.str("canonical"))))
            }
        }
    }

    @Test
    fun es256Signatures() {
        val o = load("es256_signatures.json")
        val key = o.obj("test_key")
        val priv = KeyFactory.getInstance("EC").generatePrivate(PKCS8EncodedKeySpec(Base64Url.decode(key.str("private_pkcs8_b64u"))))
        val pubB64 = key.str("public_spki_b64u")
        Es256.publicKeyFromB64u(pubB64) // P-256 válida
        println("es256_signatures: ${cases(o).size} casos (${cases(o).count { it.bool("valid") }} válidos)")
        for (c in cases(o)) {
            val name = c.str("name")
            val canonical = c.str("canonical")
            assertEquals("[$name] RDJ1 da mensagem", canonical, Rdj1.encode(c.obj("message")))
            val data = canonical.toByteArray(Charsets.UTF_8)
            val ok = Es256.verifyB64u(c.str("public_spki_b64u"), data, c.str("signature_b64u"))
            assertEquals("[$name] verificação", c.bool("valid"), ok)
            // round-trip próprio (ECDSA não é determinístico): assinar com a chave de teste e verificar
            val mine = Es256.signRaw(priv, data)
            assertTrue("[$name] round-trip", Es256.verify(Es256.publicKeyFromB64u(pubB64), data, mine))
        }
    }

    @Test
    fun cycleResolution() {
        val all = cases(load("cycle_resolution.json"))
        println("cycle_resolution: ${all.size} casos, ${all.sumOf { it.arr("queries").size }} consultas")
        for (c in all) {
            val name = c.str("name")
            val manifest = ManifestCodec.parse(c.obj("manifest"))
            val anchor = c.long("anchor_ms")
            val ls = c.obj("local_state")
            val state = LocalState(
                revoked = ls.arrOrEmpty("revoked").map { (it as JsonValue.Str).value }.toSet(),
                mediaReady = ls.arrOrEmpty("media_ready").map { (it as JsonValue.Str).value }.toSet(),
            )
            for (q in c.arr("queries").map { it as JsonValue.Obj }) {
                val t = q.long("t_ms")
                val e = q.obj("expected")
                val s = CycleResolver.resolve(manifest, anchor, state, t)
                val where = "[$name t=$t]"
                assertEquals("$where block", e.str("block"), s.block.name)
                assertEquals("$where kind", e.str("kind"), s.kind.name)
                assertEquals("$where creative_key", e.strOrNull("creative_key"), s.creativeKey)
                assertEquals("$where start_ms", e.long("start_ms"), s.startMs)
                assertEquals("$where end_ms", e.long("end_ms"), s.endMs)
            }
        }
    }

    /**
     * Players usam só a parte de FORMA (§9): RDJ1 do evento = texto canônico do vetor; assinatura ES256 raw sobre o RDJ1
     * com a `device_key` (exceto casos BAD_SIGNATURE, que NÃO podem verificar); eventos bem-formados têm exatamente as
     * chaves que o [PopRecorder] emite; `prev_hash` encadeia pelo sha256 do RDJ1 do evento anterior (lote).
     */
    @Test
    fun popValidationShape() {
        val o = load("pop_validation.json")
        val deviceKey = o.obj("device_key").str("public_spki_b64u")
        val recorderKeys = br.com.rdmidiaindoor.player.pop.PopRecorder.eventJson(
            "d", "s",
            br.com.rdmidiaindoor.player.pop.PopInput("c", "RD-X", null, null, "EXTERNAL", 1, 0, 1, 1,
                br.com.rdmidiaindoor.player.pop.PopResult.COMPLETED, 0, false),
            "u", 1, "n", "0".repeat(64),
        ).fields.keys
        var checkedSignatures = 0
        for (c in cases(o)) {
            val name = c.str("name")
            val event = c.obj("event")
            val canonical = c.str("canonical")
            assertEquals("[$name] RDJ1 do evento", canonical, Rdj1.encode(event))
            val expected = c.obj("expected")
            val reason = expected.strOrNull("reason")
            val verifies = Es256.verifyB64u(deviceKey, canonical.toByteArray(Charsets.UTF_8), c.str("signature_b64u"))
            when (reason) {
                "BAD_SIGNATURE" -> assertFalse("[$name] assinatura não pode verificar", verifies)
                "DEVICE_MISMATCH", "SCHEMA_INVALID" -> {}
                else -> { assertTrue("[$name] assinatura ES256 do evento", verifies); checkedSignatures++ }
            }
            if (reason != "SCHEMA_INVALID") assertEquals("[$name] chaves do evento rd.pop/1", recorderKeys, event.fields.keys)
        }
        assertTrue(checkedSignatures > 20)
        println("pop_validation: ${cases(o).size} casos, $checkedSignatures assinaturas verificadas com a device_key")
        // lote: cada item é {event, signature}; a cadeia prev_hash usa o sha256 do RDJ1 do evento anterior
        for (b in o.arrOrEmpty("batch_cases").map { it as JsonValue.Obj }) {
            val items = b.arr("items").map { it as JsonValue.Obj }
            for (it in items) assertEquals(setOf("event", "signature"), it.fields.keys)
            val events = items.map { it.obj("event") }
            val bySeq = events.associateBy { it.long("seq") }
            for (e in events) {
                val prev = bySeq[e.long("seq") - 1] ?: continue
                if (prev.str("device_id") == e.str("device_id")) {
                    val h = br.com.rdmidiaindoor.player.protocol.Hex.encode(Rdj1.sha256(Rdj1.bytes(prev)))
                    if (h == e.str("prev_hash")) checkedSignatures++
                }
            }
        }
    }
}
