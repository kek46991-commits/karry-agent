package ai.karry.observer

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Test

class KarryObserverApiTest {
    @Test
    fun acceptsHttpsAndLocalEmulatorServer() {
        assertEquals("https://agent.example", KarryObserverApi.validateServerUrl("https://agent.example/"))
        assertEquals("http://10.0.2.2:8000", KarryObserverApi.validateServerUrl("http://10.0.2.2:8000"))
    }

    @Test
    fun rejectsCleartextRemoteAndEmbeddedCredentials() {
        assertThrows(ObserverApiException::class.java) {
            KarryObserverApi.validateServerUrl("http://agent.example")
        }
        assertThrows(ObserverApiException::class.java) {
            KarryObserverApi.validateServerUrl("https://owner:secret@agent.example")
        }
    }

    @Test
    fun parsesLocationHistoryNewestFirst() {
        val points = KarryObserverApi.parseLocations(
            org.json.JSONArray(
                """[{"latitude":35.6895,"longitude":139.6917,"accuracy_m":12,"collected_at":2000},""" +
                    """{"latitude":35.681236,"longitude":139.767125,"accuracy_m":20,"collected_at":1000}]""",
            ),
        )
        assertEquals(2, points.size)
        assertEquals(35.6895, points[0].latitude, 0.0)
        assertEquals(1000.0, points[1].collectedAt, 0.0)
    }
}
