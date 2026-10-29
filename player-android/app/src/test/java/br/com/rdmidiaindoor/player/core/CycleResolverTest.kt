package br.com.rdmidiaindoor.player.core

import br.com.rdmidiaindoor.player.TestManifests
import br.com.rdmidiaindoor.player.TestManifests.ANCHOR_0807
import br.com.rdmidiaindoor.player.TestManifests.Creative
import br.com.rdmidiaindoor.player.TestManifests.MIN
import br.com.rdmidiaindoor.player.TestManifests.SEC
import br.com.rdmidiaindoor.player.TestManifests.WEEK
import br.com.rdmidiaindoor.player.TestManifests.local
import br.com.rdmidiaindoor.player.protocol.Manifest
import br.com.rdmidiaindoor.player.protocol.jsonOf
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** Cenários normativos RDPP §5.1/§5.2/§8.2 e A6 (âncora 08:07, reboot 08:47 e 08:49:10, 2+2, 3+1, repetição…). */
class CycleResolverTest {

    private fun resolver(m: Manifest, anchor: Long = ANCHOR_0807, revoked: Set<String> = emptySet(), ready: Set<String>? = null) =
        CycleResolver(m, anchor, LocalState(revoked, ready ?: m.media.keys))

    private fun seg(block: Block, kind: SegmentKind, key: String?, start: Long, end: Long) = listOf(block, kind, key, start, end)
    private fun Segment.tuple() = listOf(block, kind, creativeKey, startMs, endMs)

    @Test
    fun anchor0807_rebootAt0847_isInternalBlockOfCycle10Offset0() {
        val m = TestManifests.standard22().build()
        val r = resolver(m)
        val t = local("08:47:00")
        assertEquals(10L, r.cycleIndex(t))
        assertEquals(0L, r.phase(t))
        assertTrue(r.isCycleStart(t))
        // pattern[10 mod 1] = c7 (60 s) + c8 (60 s)
        assertEquals(seg(Block.INTERNAL, SegmentKind.ITEM, "c7", t, t + MIN), r.resolve(t).tuple())
        assertEquals(seg(Block.INTERNAL, SegmentKind.ITEM, "c8", t + MIN, t + 2 * MIN), r.resolve(t + 90 * SEC).tuple())
    }

    @Test
    fun anchor0807_rebootAt084910_isExternalBlockMidItem_runtimeShowsFillerAndSkipped() {
        val b = TestManifests.standard22()
        b.external(10, Triple("e1", 0, 30_000), Triple("e2", 1, 60_000), Triple("e1", 3, 30_000))
        val m = b.build()
        val r = resolver(m)
        val t = local("08:49:10")
        assertEquals(10L, r.cycleIndex(t))
        assertEquals(130 * SEC, r.phase(t))
        val s = local("08:49:00")
        // resolve() devolve o ITEM (a regra de meio de item é do runtime — §5.2)
        val item = r.resolve(t)
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "e1", s, s + 30 * SEC), item.tuple())
        assertEquals("ins-e1-10-0", item.insertionId)

        val planner = PlaybackPlanner(r)
        val d = planner.decide(t)
        assertEquals(SegmentKind.FILLER, d.segment.kind)
        assertEquals("f1", d.segment.creativeKey)
        assertEquals(t, d.startMs)
        assertEquals(local("08:49:30"), d.targetEndMs) // fallback até a próxima fronteira (fim do item)
        assertFalse(d.recordPop)
        assertEquals(listOf("e1"), d.skipped.map { it.creativeKey })
        // na fronteira seguinte o item seguinte toca normalmente (e2 de 60 s, unidades 1–2)
        val d2 = planner.decide(local("08:49:30"))
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "e2", local("08:49:30"), local("08:50:30")), d2.segment.tuple())
        assertTrue(d2.recordPop)
        assertTrue(d2.skipped.isEmpty())
        // unidade 4 (sem entrada? não — u=3 tem e1)
        assertEquals("e1", r.resolve(local("08:50:40")).creativeKey)
    }

    @Test
    fun rebootNeverRecalculatesAnchor_hourCycle0807to0907Preserved() {
        val store = InMemoryAnchorStore()
        val am = AnchorManager(store)
        val first = am.establish("screen-1", null, ANCHOR_0807 + 731) // 08:07:00.731 → arredonda ao segundo
        assertEquals(ANCHOR_0807, first)
        // "reboot" às 08:47: nova instância, mesmo armazenamento ⇒ mesma âncora
        val again = AnchorManager(store).establish("screen-1", null, local("08:47:00"))
        assertEquals(ANCHOR_0807, again)
        val r = resolver(TestManifests.standard22().build(), anchor = again)
        assertEquals(15L, r.cycleIndex(local("09:07:00")))
        assertEquals(0L, r.phase(local("09:07:00")))
    }

    @Test
    fun twoPlusTwo_fullySoldExternalBlock_and120sOccupiesWholeBlock() {
        val b = TestManifests.standard22()
        b.external(0, Triple("e1", 0, 30_000), Triple("e1", 1, 30_000), Triple("e2", 2, 60_000))
        b.external(1, Triple("e3", 0, 120_000))
        val r = resolver(b.build())
        val s0 = ANCHOR_0807 + 2 * MIN
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "e1", s0, s0 + 30 * SEC), r.resolve(s0).tuple())
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "e1", s0 + 30 * SEC, s0 + 60 * SEC), r.resolve(s0 + 45 * SEC).tuple())
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "e2", s0 + 60 * SEC, s0 + 120 * SEC), r.resolve(s0 + 61 * SEC).tuple())
        val s1 = ANCHOR_0807 + 6 * MIN
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "e3", s1, s1 + 2 * MIN), r.resolve(s1 + 100 * SEC).tuple())
        // 60 inserções de 30 s por hora no 2+2: 15 blocos externos de 4 unidades
        assertEquals(15L, 3_600_000L / m22Cycle())
    }

    private fun m22Cycle() = 240_000L

    @Test
    fun threePlusOne_rotation() {
        val b = TestManifests.Builder(ANCHOR_0807, internalMs = 180_000, externalMs = 60_000).apply {
            creative(Creative("c7", "INTERNAL", "i7", 60_000))
            creative(Creative("f1", "FALLBACK", "if1", 30_000))
            creative(Creative("e1", "EXTERNAL", "ie1", 30_000))
            internalPattern += listOf("c7" to 60_000L, "c7" to 60_000L, "c7" to 60_000L)
            fallback += "f1" to 30_000L
        }
        b.external(2, Triple("e1", 1, 30_000))
        val r = resolver(b.build())
        val k2 = ANCHOR_0807 + 2 * 4 * MIN
        assertEquals(Block.INTERNAL, r.resolve(k2 + 179 * SEC).block)
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "f1", k2 + 3 * MIN, k2 + 3 * MIN + 30 * SEC), r.resolve(k2 + 3 * MIN).tuple())
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "e1", k2 + 3 * MIN + 30 * SEC, k2 + 4 * MIN), r.resolve(k2 + 3 * MIN + 31 * SEC).tuple())
        assertEquals(Block.INTERNAL, r.resolve(k2 + 4 * MIN).block)
    }

    @Test
    fun weeklyRepetitionBeyondHorizon_usesSameBucketMinusPeriod_andValidityAgainstRealTime() {
        val b = TestManifests.standard22()
        b.horizonFrom = ANCHOR_0807 - 1
        b.horizonTo = ANCHOR_0807 + WEEK
        b.creatives["e2"] = Creative("e2", "EXTERNAL", "ve2", 60_000, validUntil = ANCHOR_0807 + WEEK + 10 * MIN)
        b.external(0, Triple("e1", 0, 30_000), Triple("e2", 2, 60_000))
        val m = b.build()
        val r = resolver(m)
        val k = WEEK / (4 * MIN) // mesmo bloco, uma semana depois
        val s = ANCHOR_0807 + k * 4 * MIN + 2 * MIN
        assertEquals(m.externalPlan.keys.first(), r.externalBucket(s))
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "e1", s, s + 30 * SEC), r.resolve(s).tuple())
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "e2", s + MIN, s + 2 * MIN), r.resolve(s + MIN).tuple())
        // duas semanas depois: e2 venceu (validade checada no t real) ⇒ fallback em unidades de 30 s
        val s2 = s + WEEK
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "e1", s2, s2 + 30 * SEC), r.resolve(s2).tuple())
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "f1", s2 + MIN, s2 + MIN + 30 * SEC), r.resolve(s2 + MIN).tuple())
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "f1", s2 + MIN + 30 * SEC, s2 + 2 * MIN), r.resolve(s2 + 95 * SEC).tuple())
        // antes do horizonte: n negativo (menor |n|)
        val sBefore = s - 2 * WEEK
        assertEquals(m.externalPlan.keys.first(), r.externalBucket(sBefore))
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "e1", sBefore, sBefore + 30 * SEC), r.resolve(sBefore).tuple())
    }

    @Test
    fun expiredCreative_goesToFallback_offlineValidity() {
        val b = TestManifests.standard22()
        b.creatives["c8"] = Creative("c8", "INTERNAL", "i8", 60_000, validUntil = local("08:10:00"))
        val r = resolver(b.build())
        // ciclo 0 começa 08:07; c8 começa 08:08 (< 08:10) ⇒ toca
        assertEquals("c8", r.resolve(local("08:08:30")).creativeKey)
        // ciclo 1 (08:11): c8 começaria 08:12 ≥ valid_until ⇒ fallback (2 × f1)
        val s = local("08:12:00")
        assertEquals(seg(Block.INTERNAL, SegmentKind.ITEM, "f1", s, s + 30 * SEC), r.resolve(s).tuple())
        assertEquals(seg(Block.INTERNAL, SegmentKind.ITEM, "f1", s + 30 * SEC, s + MIN), r.resolve(s + 40 * SEC).tuple())
        // valid_from no futuro também não toca
        val b2 = TestManifests.standard22()
        b2.creatives["c7"] = Creative("c7", "INTERNAL", "i7", 60_000, validFrom = local("09:00:00"))
        assertEquals("f1", resolver(b2.build()).resolve(local("08:47:00")).creativeKey)
    }

    @Test
    fun validityBoundaries_validFromInclusive_validUntilExclusive() {
        val b = TestManifests.standard22()
        val s = ANCHOR_0807 + 2 * MIN
        b.creatives["e1"] = Creative("e1", "EXTERNAL", "ie1", validFrom = s, validUntil = s + 30 * SEC)
        b.external(0, Triple("e1", 0, 30_000), Triple("e1", 1, 30_000))
        val r = resolver(b.build())
        assertEquals("e1", r.resolve(s).creativeKey) // início == valid_from ⇒ toca
        assertEquals("f1", r.resolve(s + 30 * SEC).creativeKey) // início == valid_until ⇒ não toca
        assertEquals("EXPIRED", r.ineligibility("e1", s + 30 * SEC))
        assertEquals("NOT_YET_VALID", r.ineligibility("e1", s - 1))
    }

    @Test
    fun killedOrRevokedOrMediaNotReady_goToFallback() {
        val b = TestManifests.standard22()
        b.external(0, Triple("e1", 0, 30_000), Triple("e2", 1, 60_000))
        b.revoked += "id-e2"
        val m = b.build()
        val s = ANCHOR_0807 + 2 * MIN
        val r = resolver(m, revoked = setOf("id-e1"))
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "f1", s, s + 30 * SEC), r.resolve(s).tuple()) // kill local
        assertEquals("f1", r.resolve(s + 31 * SEC).creativeKey) // revoked do manifesto
        // mídia não pronta (falha permanente de download) ⇒ fallback
        val r2 = resolver(m, ready = m.media.keys - "ie1")
        assertEquals("f1", r2.resolve(s).creativeKey)
        assertEquals("REVOKED", resolver(m).ineligibility("e2", s + 30 * SEC))
        assertEquals("MEDIA_NOT_READY", r2.ineligibility("e1", s))
    }

    @Test
    fun fallbackFill_cyclicFromUnitIndex_onlyWholeItems_restIsFiller() {
        val b = TestManifests.Builder(ANCHOR_0807).apply {
            creative(Creative("f1", "FALLBACK", "if1", 30_000))
            creative(Creative("f2", "FALLBACK", "if2", 60_000))
            fallback += "f1" to 30_000L
            fallback += "f2" to 60_000L
        }
        val r = resolver(b.build())
        // internal_pattern vazio ⇒ bloco interno todo em fallback, índice inicial floor((s−A)/U) mod 2
        val out = ArrayList<Segment>()
        r.fill(ANCHOR_0807, ANCHOR_0807 + 2 * MIN, Block.INTERNAL, out)
        assertEquals(listOf("f1", "f2", "f1"), out.map { it.creativeKey })
        assertEquals(listOf(SegmentKind.ITEM, SegmentKind.ITEM, SegmentKind.ITEM), out.map { it.kind })
        // intervalo de 30 s começando na unidade 1 (índice 1 = f2 de 60 s): não cabe ⇒ FILLER com f2, sem PoP
        val out2 = ArrayList<Segment>()
        r.fill(ANCHOR_0807 + 30 * SEC, ANCHOR_0807 + 60 * SEC, Block.EXTERNAL, out2)
        assertEquals(listOf(SegmentKind.FILLER), out2.map { it.kind })
        assertEquals("f2", out2[0].creativeKey)
        // fallback vazio ⇒ FILLER com creative_key null (tela institucional embutida)
        val empty = TestManifests.Builder(ANCHOR_0807).build()
        val e = resolver(empty).resolve(ANCHOR_0807 + 10 * SEC)
        assertEquals(seg(Block.INTERNAL, SegmentKind.FILLER, null, ANCHOR_0807, ANCHOR_0807 + 2 * MIN), e.tuple())
    }

    @Test
    fun internalPatternRotation_kModLen_andItemCrossingBlockEndDoesNotPlay() {
        val b = TestManifests.standard22()
        b.creatives["c9"] = Creative("c9", "INTERNAL", "i9", 120_000)
        b.internalPattern.clear()
        b.internalPattern += listOf("c7" to 60_000L, "c8" to 60_000L)
        b.internalPattern += listOf("c8" to 60_000L, "c9" to 120_000L) // c9 ultrapassa o bloco
        val r = resolver(b.build())
        assertEquals("c7", r.resolve(ANCHOR_0807).creativeKey) // k=0 → pattern[0]
        val k1 = ANCHOR_0807 + 4 * MIN
        assertEquals("c8", r.resolve(k1).creativeKey) // k=1 → pattern[1]
        assertEquals(seg(Block.INTERNAL, SegmentKind.ITEM, "f1", k1 + MIN, k1 + MIN + 30 * SEC), r.resolve(k1 + MIN).tuple())
        assertEquals("c7", r.resolve(ANCHOR_0807 + 8 * MIN).creativeKey) // k=2 → pattern[0]
    }

    @Test
    fun externalEntryOverlappingOrExceedingBlock_isDefendedWithFallback() {
        val b = TestManifests.standard22()
        b.external(0, Triple("e1", 0, 30_000), Triple("e2", 0, 60_000), Triple("e3", 2, 120_000))
        val r = resolver(b.build())
        val s = ANCHOR_0807 + 2 * MIN
        assertEquals("e1", r.resolve(s).creativeKey) // a primeira do par sobreposto toca
        // e2 (u=0, 60 s) sobrepõe e1: só o trecho [s+30, s+60) vira fallback
        assertEquals(seg(Block.EXTERNAL, SegmentKind.ITEM, "f1", s + 30 * SEC, s + 60 * SEC), r.resolve(s + 30 * SEC).tuple())
        // e3 (u=2, 120 s) ultrapassa o fim do bloco: [s+60, s+120) em fallback
        assertEquals("f1", r.resolve(s + 61 * SEC).creativeKey)
        assertEquals("f1", r.resolve(s + 91 * SEC).creativeKey)
    }

    @Test
    fun emergencyPreemptsEverything_lastPartialRepetitionIsFiller_andPreemptedItemsAreSkipped() {
        val b = TestManifests.standard22()
        b.creative(Creative("x1", "INSTITUTIONAL", "ix1", 30_000))
        b.external(0, Triple("e1", 0, 30_000))
        val from = ANCHOR_0807 + 90 * SEC
        val until = from + 75 * SEC
        b.emergency += jsonOf("id" to "em-1", "c" to "x1", "from_ms" to from, "until_ms" to until)
        val r = resolver(b.build())
        assertEquals(seg(Block.EMERGENCY, SegmentKind.ITEM, "x1", from, from + 30 * SEC), r.resolve(from + 5 * SEC).tuple())
        assertEquals(seg(Block.EMERGENCY, SegmentKind.ITEM, "x1", from + 30 * SEC, from + 60 * SEC), r.resolve(from + 31 * SEC).tuple())
        assertEquals(seg(Block.EMERGENCY, SegmentKind.FILLER, "x1", from + 60 * SEC, until), r.resolve(from + 61 * SEC).tuple())
        assertNull(r.emergencyAt(until))

        val planner = PlaybackPlanner(r)
        val d = planner.decide(from) // c8 (08:08–08:09) preemptado
        assertEquals(Block.EMERGENCY, d.segment.block)
        assertTrue(d.recordPop)
        assertEquals(listOf("c8"), d.skipped.map { it.creativeKey })
        val d2 = planner.decide(from + 30 * SEC) // e1 (08:09:00–08:09:30) preemptado
        assertTrue(d2.recordPop)
        assertEquals(listOf("e1"), d2.skipped.map { it.creativeKey })
        val d2b = planner.decide(from + 60 * SEC) // última repetição incompleta = FILLER (sem PoP); só fallback por baixo
        assertFalse(d2b.recordPop)
        assertTrue(d2b.skipped.isEmpty())
        // retorno da emergência às 08:09:45, no meio do item de fallback f1 [08:09:30, 08:10:00): FILLER + SKIPPED (§5.2)
        val d3 = planner.decide(until)
        assertEquals(SegmentKind.FILLER, d3.segment.kind)
        assertEquals(listOf("f1"), d3.skipped.map { it.creativeKey })
    }

    @Test
    fun deterministic_sameInputSameOutput() {
        val b = TestManifests.standard22()
        b.external(3, Triple("e2", 0, 60_000), Triple("e1", 2, 30_000))
        val m = b.build()
        val t0 = ANCHOR_0807
        val a = (0 until 500).map { resolver(m).resolve(t0 + it * 7_919L).tuple() }
        val c = (0 until 500).map { resolver(m).resolve(t0 + it * 7_919L).tuple() }
        assertEquals(a, c)
        // cobertura contínua: segmentos encadeados sem lacunas nem sobreposição ao longo de 2 h
        val r = resolver(m)
        var t = t0
        while (t < t0 + 2 * 60 * MIN) {
            val s = r.resolve(t)
            assertEquals(t, s.startMs)
            assertTrue(s.endMs > s.startMs)
            t = s.endMs
        }
    }
}
