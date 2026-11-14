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
}
