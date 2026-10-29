package br.com.rdmidiaindoor.player.core

import br.com.rdmidiaindoor.player.TestManifests
import br.com.rdmidiaindoor.player.TestManifests.ANCHOR_0807
import br.com.rdmidiaindoor.player.TestManifests.MIN
import br.com.rdmidiaindoor.player.TestManifests.SEC
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** Regras de runtime: atraso de troca, absorção em conteúdo não comissionado, SKIPPED único, PoP sem sobreposição. */
class PlaybackPlannerTest {
    private fun planner(cycles: Int = 1): PlaybackPlanner {
        val b = TestManifests.standard22()
        for (k in 0 until cycles) b.external(k.toLong(), Triple("e1", 0, 30_000), Triple("e1", 1, 30_000), Triple("e2", 2, 60_000))
        val m = b.build()
        return PlaybackPlanner(CycleResolver(m, ANCHOR_0807, LocalState(emptySet(), m.media.keys)))
    }

    @Test
    fun externalItemStartedLate_alwaysGetsFullDuration() {
        val p = planner()
        val s = ANCHOR_0807 + 2 * MIN
        val d = p.decide(s + 120) // 120 ms atrasado
        assertEquals("e1", d.segment.creativeKey)
        assertTrue(d.recordPop)
        assertEquals(s + 120 + 30 * SEC, d.targetEndMs) // d completo (100 %), mesmo atrasado
        assertEquals(30 * SEC, d.expectedMs)
    }

    @Test
    fun internalItem_absorbsAccumulatedLatenessAboveThreshold() {
        val p = planner()
        val d = p.decide(ANCHOR_0807 + 300) // c7 interno, 300 ms atrasado (≥ 250 ms)
        assertEquals("c7", d.segment.creativeKey)
        assertEquals(ANCHOR_0807 + MIN, d.targetEndMs) // termina na fronteira absoluta: atraso zerado
        val small = planner().decide(ANCHOR_0807 + 40) // atraso pequeno: d completo
        assertEquals(ANCHOR_0807 + 40 + MIN, small.targetEndMs)
    }

    @Test
    fun lateBeyondJoinTolerance_isMidItem_fillerUntilEnd_skippedOnce() {
        val p = planner()
        val s = ANCHOR_0807 + 2 * MIN
        val d = p.decide(s + 1_600)
        assertEquals(SegmentKind.FILLER, d.segment.kind)
        assertFalse(d.recordPop)
        assertEquals(s + 30 * SEC, d.targetEndMs)
        assertEquals(listOf("e1"), d.skipped.map { it.creativeKey })
        val again = p.decide(s + 5_000) // mesma perda não gera SKIPPED duplicado
        assertTrue(again.skipped.isEmpty())
    }

    @Test
    fun itemThatStartedAndThenFailed_isNotReportedAsSkipped() {
        val p = planner()
        val s = ANCHOR_0807 + 2 * MIN
        val d = p.decide(s)
        p.markStarted(d.segment)
        val afterError = p.decide(s + 10 * SEC) // erro/kill no meio: filler, sem SKIPPED (o PoP do item já é ERROR/INTERRUPTED)
        assertEquals(SegmentKind.FILLER, afterError.segment.kind)
        assertTrue(afterError.skipped.isEmpty())
    }

    @Test
    fun simulatedPlayback_lateSwitches_neverOverlap_andProtectedItemsAlwaysComplete() {
        val p = planner(cycles = 100)
        var t = ANCHOR_0807
        var prevEnd = Long.MIN_VALUE
        var completedExternal = 0
        var drift = 0L
        repeat(400) { i ->
            val d = p.decide(t)
            assertTrue("início não pode anteceder o fim anterior", d.startMs >= prevEnd)
            val jitter = (i % 7).toLong() // 0–6 ms de atraso de troca
            val end = d.targetEndMs + jitter
            if (d.recordPop && p.isProtectedKind(p.resolver.manifest.creatives[d.segment.creativeKey!!]?.kind)) {
                assertTrue(end - d.startMs >= d.expectedMs)
                completedExternal++
            }
            prevEnd = end
            drift = maxOf(drift, d.lateMs)
            t = end
        }
        assertTrue(completedExternal > 50)
        assertTrue("atraso acumulado deve ficar limitado (absorção): $drift", drift < 1_000)
    }
}
