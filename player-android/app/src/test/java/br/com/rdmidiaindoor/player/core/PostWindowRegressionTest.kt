package br.com.rdmidiaindoor.player.core

import br.com.rdmidiaindoor.player.TestManifests
import br.com.rdmidiaindoor.player.TestManifests.ANCHOR_0807
import br.com.rdmidiaindoor.player.TestManifests.MIN
import br.com.rdmidiaindoor.player.TestManifests.SEC
import br.com.rdmidiaindoor.player.TestManifests.local
import br.com.rdmidiaindoor.player.config.DeviceStateStore
import br.com.rdmidiaindoor.player.net.PlayerSocket
import br.com.rdmidiaindoor.player.pop.PopResult
import br.com.rdmidiaindoor.player.pop.PopRules
import br.com.rdmidiaindoor.player.protocol.JsonValue
import kotlinx.coroutines.awaitCancellation
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * Regressões equivalentes aos defeitos achados no E2E REAL do player web contra o backend (pós-janela):
 * 1) cadeia de PoP após correção do relógio; 2) liberação do kill local após lift; 3) boot pareado sem internet;
 * 4) instância única; (5 — set_tech_pin — está em ManifestAndMessagesTest).
 */
class PostWindowRegressionTest {
    @get:Rule
    val tmp = TemporaryFolder()

    // ------------------------------------------------------------------ 1) cadeia de PoP × relógio
    private data class Rec(val key: String, val kind: String?, val start: Long, val actual: Long, val expected: Long, val result: PopResult, val segStart: Long) {
        val end get() = start + actual
    }

    private class Shown(val d: Decision, val mono: Long, val showWall: Long, val popStart: Long) {
        var trim = 0L
    }

    /**
     * Simula o motor do [br.com.rdmidiaindoor.player.player.PlayerController] com as MESMAS regras puras
     * ([PlaybackPlanner], [PlaybackTiming], [PopChain], [PopRules]): relógio corrigido = parede + offset, troca com
     * atraso de 0–6 ms, offset alterado em [changeAtMono] por [delta] (negativo = correção para trás).
     */
    private fun simulate(delta: Long, changeAtMono: Long, items: Int = 160): Pair<List<Rec>, List<Decision>> {
        val b = TestManifests.standard22()
        for (k in 0 until 60L) b.external(k, Triple("e1", 0, 30_000), Triple("e1", 1, 30_000), Triple("e2", 2, 60_000))
        val m = b.build()
        val planner = PlaybackPlanner(CycleResolver(m, ANCHOR_0807, LocalState(emptySet(), m.media.keys)))
        val wall0 = ANCHOR_0807 // boot exatamente na âncora
        var mono = 0L
        var offset = 0L
        fun now() = wall0 + mono + offset
        val recs = ArrayList<Rec>()
        val decisions = ArrayList<Decision>()
        var cur: Shown? = null
        var i = 0
        while (i < items) {
            if (offset == 0L && mono >= changeAtMono) offset = delta
            val t = now()
            val c = cur
            val ext = c?.let { PlaybackTiming.extensionMs(it.d.segment.endMs, t) }
            if (c != null && ext != null) {
                mono += ext + (i % 3)
                continue
            }
            val d = planner.decide(t)
            if (d.segment.kind == SegmentKind.ITEM) planner.markStarted(d.segment)
            decisions += d
            var popStart = t
            if (c != null && c.d.recordPop) {
                val link = PopChain.link(c.popStart, mono - c.mono, c.d.expectedMs, t)
                c.trim = link.trimPreviousMs
                popStart = link.popStartMs
            }
            if (c != null && c.d.recordPop) {
                val actual = mono - c.mono - c.trim
                recs += Rec(c.d.segment.creativeKey!!, m.creatives[c.d.segment.creativeKey!!]?.kind, c.popStart, actual, c.d.expectedMs,
                    PopRules.result(c.d.expectedMs, actual, endedNaturally = true, interrupted = false, error = false), c.d.segment.startMs)
            }
            cur = Shown(d, mono, t, popStart)
            mono += (d.targetEndMs - t).coerceAtLeast(0) + (i % 7) // jitter de troca 0–6 ms
            i++
        }
        return recs to decisions
    }

    private fun assertChainHealthy(recs: List<Rec>, decisions: List<Decision>, label: String) {
        for (k in 1 until recs.size) {
            assertTrue("[$label] sobreposição de PoP em ${recs[k]}", recs[k].start >= recs[k - 1].end)
        }
        val external = recs.filter { it.kind == "EXTERNAL" }
        assertTrue(external.size > 40)
        external.forEach { assertEquals("[$label] externo não COMPLETED: $it", PopResult.COMPLETED, it.result) }
        // sem FILLER "relâmpago" (< 1 s) causado pela correção do relógio
        decisions.filter { it.segment.kind == SegmentKind.FILLER }.forEach {
            assertTrue("[$label] FILLER relâmpago ${it.targetEndMs - it.startMs} ms", it.targetEndMs - it.startMs >= 1_000)
        }
    }

    @Test
    fun clockCorrectedBackwards2480ms_chainStaysContiguous_noPermanentShift_externalsComplete() {
        val (recs, decisions) = simulate(delta = -2_480, changeAtMono = 3 * MIN + 10 * SEC)
        assertChainHealthy(recs, decisions, "-2480")
        // a base não se propaga: itens bem depois da correção registram o início na fronteira (± jitter/absorção)
        val late = recs.drop(recs.size / 2)
        late.forEach { assertTrue("início deslocado: ${it.start - it.segStart} ms", it.start - it.segStart in 0..600) }
        // o item que atravessou a correção continua 100 % (o excesso é descontado só do registro)
        assertTrue(recs.none { it.result != PopResult.COMPLETED && it.kind == "EXTERNAL" })
    }

    @Test
    fun clockCorrectedForward_andSmallJitterBothWays_noOverlap() {
        val (r1, d1) = simulate(delta = +800, changeAtMono = 2 * MIN + 5 * SEC)
        assertChainHealthy(r1, d1, "+800")
        val (r2, d2) = simulate(delta = -40, changeAtMono = 2 * MIN + 5 * SEC)
        assertChainHealthy(r2, d2, "-40")
    }

    @Test
    fun popChainLink_trimsOnlyTheExcess_andShiftsRemainder() {
        // anterior começou em 1000, mediu 32 480 para d = 30 000; relógio voltou 2 480 ⇒ novo show em 30 000 + 1000
        val l = PopChain.link(prevStartMs = 1_000, prevMeasuredMs = 32_480, prevExpectedMs = 30_000, showWallMs = 31_000)
        assertEquals(2_480L, l.trimPreviousMs)
        assertEquals(31_000L, l.popStartMs)
        // sem excesso suficiente: só parte é descontada, o resto desloca o início do novo registro
        val l2 = PopChain.link(1_000, 30_100, 30_000, 30_000)
        assertEquals(100L, l2.trimPreviousMs)
        assertEquals(31_000L, l2.popStartMs)
        // relógio para frente: nenhuma sobreposição, nada a descontar
        assertEquals(PopChain.Link(40_000, 0), PopChain.link(1_000, 30_000, 30_000, 40_000))
        assertNull(PlaybackTiming.extensionMs(10_000, 10_000))
        assertEquals(2_480L, PlaybackTiming.extensionMs(10_000, 7_520))
        assertNull(PlaybackTiming.extensionMs(10_000, 4_000)) // > 5 s: FILLER até a fronteira
    }

    @Test
    fun clockService_ignoresJitter_appliesRealJumps_andTracksContact() {
        var mono = 0L
        val c = ClockService({ 1_000_000L }, { mono })
        assertFalse(c.contactedWithin(90_000))
        c.onServerTime(1_002_480, 1_000_000, 1_000_000) // 1ª amostra (ruim): +2 480
        assertEquals(2_480L, c.offsetMs)
        c.onServerTime(1_000_100, 1_000_000, 1_000_000) // correção real para trás (salto ≥ 150 ms, RTT bom)
        assertEquals(100L, c.offsetMs)
        c.onServerTime(1_000_160, 1_000_000, 1_000_000) // jitter < 150 ms: ignorado
        assertEquals(100L, c.offsetMs)
        c.onServerTime(1_001_000, 1_000_000, 1_001_200) // RTT ruim (1 200 ms) e salto < 2 s: ignorado
        assertEquals(100L, c.offsetMs)
        c.onServerTime(1_010_000, 1_000_000, 1_001_000) // salto grande (relógio ajustado): aplicado mesmo com RTT ruim
        assertEquals(9_500L, c.offsetMs)
        assertTrue(c.contactedWithin(90_000))
        mono = 91_000
        assertFalse(c.contactedWithin(90_000))
    }

    // ------------------------------------------------------------------ 2) kill local × lift
    @Test
    fun killIsPersisted_andLiftedOnlyByManifestGeneratedAfterIt_withoutTheCreativeInRevoked() {
        val file = tmp.newFile("state.json").apply { delete() }
        val s = DeviceStateStore(file)
        val killAt = 1_791_800_000_000L
        assertTrue(s.addKilled(listOf("cr-1", "cr-2"), killAt))
        assertFalse(s.addKilled(listOf("cr-1"), killAt + 5))
        // reboot: persiste
        val s2 = DeviceStateStore(file)
        assertEquals(setOf("cr-1", "cr-2"), s2.killed)
        // manifesto gerado ANTES (ou dentro da margem de 2 s) não libera
        assertTrue(s2.reconcileKills(emptySet(), killAt + 1_500).isEmpty())
        // manifesto gerado depois e com cr-2 ainda em revoked: só cr-1 é liberado
        assertEquals(setOf("cr-1"), s2.reconcileKills(setOf("cr-2"), killAt + 60_000))
        assertEquals(setOf("cr-2"), DeviceStateStore(file).killed)
        // o resolvedor volta a tocar o criativo liberado
        val b = TestManifests.standard22()
        b.creatives["e1"] = TestManifests.Creative("e1", "EXTERNAL", "ie1", id = "cr-1")
        b.external(0, Triple("e1", 0, 30_000))
        val m = b.build()
        val s0 = ANCHOR_0807 + 2 * MIN
        assertEquals("f1", CycleResolver(m, ANCHOR_0807, LocalState(setOf("cr-1"), m.media.keys)).resolve(s0).creativeKey)
        assertEquals("e1", CycleResolver(m, ANCHOR_0807, LocalState(s2.killed, m.media.keys)).resolve(s0).creativeKey)
    }

    @Test
    fun legacyKillListFormat_isReadAndLiftable() {
        val file = tmp.newFile("legacy.json")
        file.writeText("""{"killed":["cr-old"]}""")
        val s = DeviceStateStore(file)
        assertEquals(setOf("cr-old"), s.killed)
        assertEquals(setOf("cr-old"), s.reconcileKills(emptySet(), 10_000))
        assertTrue(DeviceStateStore(file).killed.isEmpty())
        assertEquals(setOf("x"), KillReconciler.lifted(mapOf("x" to 0L, "y" to 0L), setOf("y"), 2_001))
        assertTrue(KillReconciler.lifted(mapOf("x" to 0L), emptySet(), 2_000).isEmpty())
    }

    // ------------------------------------------------------------------ 3) boot pareado sem internet
    @Test
    fun pairedBootWithoutInternet_playsFromCacheImmediately_fillerAndSkipped_offlineFlag() {
        val file = tmp.newFile("boot.json").apply { delete() }
        val st = DeviceStateStore(file)
        st.setIfAbsent("screen-1", ANCHOR_0807) // âncora persistida no boot anterior
        val b = TestManifests.standard22()
        b.external(10, Triple("e1", 0, 30_000), Triple("e2", 1, 60_000))
        val m = b.build() // manifesto ativo em cache (anchor_ms null no manifesto)
        val anchor = AnchorManager(DeviceStateStore(file)).known(m.screenId, m.anchorMs)
        assertEquals(ANCHOR_0807, anchor)
        val planner = PlaybackPlanner(CycleResolver(m, anchor!!, LocalState(st.killed, m.media.keys)))
        val t = local("08:49:10")
        val d = planner.decide(t)
        // entra direto com FILLER de fallback (não a tela institucional de espera) + SKIPPED do item perdido
        assertEquals(SegmentKind.FILLER, d.segment.kind)
        assertEquals("f1", d.segment.creativeKey)
        assertEquals(listOf("e1"), d.skipped.map { it.creativeKey })
        assertEquals(local("08:49:30"), d.targetEndMs)
        // sem nenhum contato com o servidor neste boot ⇒ PoP com offline = true
        val clock = ClockService({ t }, { 0L })
        assertFalse(clock.contactedWithin(90_000))
    }

    // ------------------------------------------------------------------ 4) instância única
    @Test
    fun singleOwner_newClaimEvictsPrevious() {
        val o = SingleOwner<Any>()
        val a = Any()
        val b = Any()
        assertNull(o.claim(a))
        assertNull(o.claim(a)) // reentrante
        assertSame(a, o.claim(b))
        assertFalse(o.isOwner(a))
        o.release(a) // o despejado não libera o novo dono
        assertTrue(o.isOwner(b))
        o.release(b)
        assertFalse(o.isOwner(b))
    }

    @Test
    fun playerSocket_startTwice_runsASingleConnectionLoop() = runTest {
        var tickets = 0
        val listener = object : PlayerSocket.Listener {
            override fun heartbeat() = JsonValue.Obj(emptyMap())
            override fun telemetry() = JsonValue.Obj(emptyMap())
        }
        val socket = PlayerSocket(OkHttpClient(), backgroundScope, { "ws://localhost/ws/player?ticket=$it" }, {
            tickets++
            awaitCancellation()
        }, { 0L }, listener)
        socket.start()
        socket.start()
        runCurrent()
        assertEquals(1, tickets)
        socket.stop()
    }
}
