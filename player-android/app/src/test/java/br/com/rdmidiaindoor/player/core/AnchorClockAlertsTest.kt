package br.com.rdmidiaindoor.player.core

import br.com.rdmidiaindoor.player.TestManifests.DAY
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class AnchorClockAlertsTest {

    @Test
    fun anchor_writeOnceLocal_serverAuthoritative() {
        val store = InMemoryAnchorStore()
        val am = AnchorManager(store)
        assertNull(am.known("s1", null))
        assertEquals(1_791_380_000_000L, am.establish("s1", null, 1_791_380_000_999L))
        assertTrue(am.needsPost("s1"))
        // segundo "start válido" (reboot) não recalcula
        assertEquals(1_791_380_000_000L, am.establish("s1", null, 1_791_390_000_000L))
        // servidor write-once devolve valor existente diferente ⇒ player adota
        am.onServerResponse("s1", 1_791_379_000_000L)
        assertFalse(am.needsPost("s1"))
        assertEquals(1_791_379_000_000L, am.known("s1", null))
        // manifesto com anchor_ms prevalece
        assertEquals(42_000L, am.known("s1", 42_000L))
        assertEquals(42_000L, store.get("s1"))
        assertEquals(-1000L, AnchorManager.floorToSecond(-1L))
    }

    @Test
    fun clock_offsetFromServerTime_withHalfRtt() {
        var wall = 1_000_000L
        val c = ClockService({ wall }, { 0L })
        c.onServerTime(serverMs = 1_100_200L, sentLocalMs = 1_000_000L, receivedLocalMs = 1_000_400L)
        assertEquals(100_000L, c.offsetMs) // 1 100 200 − (1 000 000 + 200)
        wall = 2_000_000L
        assertEquals(2_100_000L, c.now())
        assertTrue(c.measuredThisBoot)
    }

    @Test
    fun clockAlert_beforeGeneratedAt_orLargeOffsetWithoutMeasurement() {
        val c = ClockService({ 1_000L }, { 0L })
        assertTrue(c.clockAlert(activeGeneratedAtMs = 5_000L))
        assertFalse(c.clockAlert(activeGeneratedAtMs = 500L))
        val stale = ClockService({ 10_000_000L }, { 0L }, initialOffsetMs = 200_000L)
        assertTrue(stale.clockAlert(null)) // offset > 120 s sem rede para corrigir
        stale.onServerTime(10_200_000L, 10_000_000L, 10_000_000L)
        assertFalse(stale.clockAlert(null)) // corrigido pelo servidor nesta sessão
    }

    @Test
    fun offlineAlerts_7daysFooter_28daysCenter_clearedAfterSync() {
        val last = 1_000_000L
        val days = listOf(7, 28)
        assertEquals(OfflineAlerts.Level.NONE, OfflineAlerts.level(last + 6 * DAY + DAY - 1, last, days))
        assertEquals(OfflineAlerts.Level.FOOTER, OfflineAlerts.level(last + 7 * DAY, last, days))
        assertEquals(OfflineAlerts.Level.CENTER, OfflineAlerts.level(last + 28 * DAY, last, days))
        assertEquals(OfflineAlerts.Level.NONE, OfflineAlerts.level(last + 28 * DAY, last + 28 * DAY - 10, days)) // sincronizou
        assertEquals(OfflineAlerts.Level.NONE, OfflineAlerts.level(last, null, days))
        assertEquals(30L, OfflineAlerts.daysWithoutSync(last + 30 * DAY + 5, last))
    }
}
