package ai.karry.observer

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class SetupStatusTest {
    private fun snapshot(
        enrolled: Boolean = false,
        token: Boolean = false,
        urlValid: Boolean = true,
        reachable: Boolean? = true,
    ) = SetupStatus.Snapshot("Karry-Agent 1", "ai.karry.observer", urlValid, token, enrolled, reachable, true)

    @Test
    fun linesAreRawEnglishYesNo() {
        val lines = SetupStatus.lines(snapshot())
        assertEquals("app: Karry-Agent 1 (ai.karry.observer)", lines.first())
        assertTrue(lines.contains("runtime_permissions_required: no"))
        assertTrue(lines.contains("backend_reachable: yes"))
        assertTrue(lines.contains("enrolled: no -> enter owner token, then Yes"))
        assertTrue(lines.contains("background_actions: no (observer only, foreground polling)"))
        assertEquals("ready: no", lines.last())
        assertEquals("ready: yes", SetupStatus.lines(snapshot(enrolled = true)).last())
        assertTrue(SetupStatus.lines(snapshot(reachable = null)).contains("backend_reachable: checking"))
    }

    @Test
    fun promptOnlyAsksWhenEnrollmentCanProceed() {
        assertEquals("enroll: waiting for owner token above", SetupStatus.prompt(snapshot(), skipped = false))
        assertEquals("enroll: register this observer now?", SetupStatus.prompt(snapshot(token = true), skipped = false))
        assertTrue(SetupStatus.prompt(snapshot(urlValid = false), skipped = false)!!.startsWith("server_url: invalid"))
        assertNull(SetupStatus.prompt(snapshot(enrolled = true), skipped = false))
        assertNull(SetupStatus.prompt(snapshot(token = true), skipped = true))
    }

    @Test
    fun unreachableBackendIsNotReady() {
        assertFalse(SetupStatus.ready(snapshot(enrolled = true, reachable = false)))
        assertTrue(SetupStatus.ready(snapshot(enrolled = true, reachable = null)))
    }
}
