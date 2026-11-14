package ai.karry.observer

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL

class KarryObserverApi {
    suspend fun enroll(
        serverUrl: String,
        ownerToken: String,
        observerId: String,
        operationId: String,
    ): Registration = withContext(Dispatchers.IO) {
        val base = validateServerUrl(serverUrl)
        val body = JSONObject()
            .put("device_id", observerId)
            .put("role", "observer")
            .put("observed_device_id", operationId)
        val response = request(
            "$base/api/mobile-agent/devices/enroll",
            method = "POST",
            bearerToken = ownerToken,
            body = body,
        )
        Registration(base, observerId, operationId, response.getString("device_token"))
    }

    suspend fun snapshot(registration: Registration): ObserverSnapshot = withContext(Dispatchers.IO) {
        val path = "/api/mobile-agent/observers/${encodeId(registration.observerId)}" +
            "/devices/${encodeId(registration.operationId)}"
        val actions = request(
            "${registration.serverUrl}$path/actions?limit=30",
            deviceToken = registration.deviceToken,
        ).getJSONArray("actions")
        val diagnostics = request(
            "${registration.serverUrl}$path/diagnostics?limit=30",
            deviceToken = registration.deviceToken,
        ).getJSONArray("diagnostics")
        val locations = request(
            "${registration.serverUrl}$path/location/history?limit=50",
            deviceToken = registration.deviceToken,
        ).getJSONArray("locations")
        ObserverSnapshot(
            actions = parseActions(actions),
            diagnostics = parseDiagnostics(diagnostics),
            locations = parseLocations(locations),
        )
    }

    /** Any HTTP answer from the server root counts as reachable; nothing is sent. */
    suspend fun reachable(serverUrl: String): Boolean = withContext(Dispatchers.IO) {
        val base = try { validateServerUrl(serverUrl) } catch (_: ObserverApiException) { return@withContext false }
        try {
            val connection = URL("$base/").openConnection() as HttpURLConnection
            try {
                connection.requestMethod = "GET"
                connection.connectTimeout = 5_000
                connection.readTimeout = 5_000
                connection.responseCode in 100..599
            } finally {
                connection.disconnect()
            }
        } catch (_: Exception) {
            false
        }
    }

    private fun request(
        url: String,
        method: String = "GET",
        bearerToken: String? = null,
        deviceToken: String? = null,
        body: JSONObject? = null,
    ): JSONObject {
        val connection = URL(url).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = method
            connection.connectTimeout = 10_000
            connection.readTimeout = 10_000
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("Cache-Control", "no-store")
            bearerToken?.let { connection.setRequestProperty("Authorization", "Bearer $it") }
            deviceToken?.let { connection.setRequestProperty("X-Device-Token", it) }
            if (body != null) {
                connection.doOutput = true
                connection.setRequestProperty("Content-Type", "application/json")
                connection.outputStream.use { it.write(body.toString().toByteArray(Charsets.UTF_8)) }
            }
            val status = connection.responseCode
            val stream = if (status in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
            if (status !in 200..299) {
                val detail = runCatching { JSONObject(text).optString("detail") }.getOrNull()
                throw ObserverApiException(detail?.takeIf { it.isNotBlank() } ?: "HTTP $status")
            }
            return JSONObject(text)
        } finally {
            connection.disconnect()
        }
    }

    companion object {
        fun validateServerUrl(value: String): String {
            val uri = runCatching { URI(value.trim()) }.getOrNull()
                ?: throw ObserverApiException("Karry-Agent URLが不正です")
            val localEmulator = uri.scheme == "http" && uri.host == "10.0.2.2"
            if ((uri.scheme != "https" && !localEmulator) || uri.host.isNullOrBlank() || uri.userInfo != null) {
                throw ObserverApiException("HTTPS URLを指定してください")
            }
            return value.trim().trimEnd('/')
        }

        private fun encodeId(value: String): String {
            if (!Regex("^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$").matches(value)) {
                throw ObserverApiException("端末IDが不正です")
            }
            return value
        }

        private fun parseActions(items: JSONArray): List<ActionStatus> = buildList {
            for (index in 0 until items.length()) {
                val item = items.getJSONObject(index)
                add(
                    ActionStatus(
                        id = item.getString("id"),
                        action = item.getString("action"),
                        status = item.getString("status"),
                        updatedAt = item.optDouble("updated_at", 0.0),
                    ),
                )
            }
        }

        internal fun parseLocations(items: JSONArray): List<LocationPoint> = buildList {
            for (index in 0 until items.length()) {
                val item = items.getJSONObject(index)
                add(
                    LocationPoint(
                        latitude = item.getDouble("latitude"),
                        longitude = item.getDouble("longitude"),
                        accuracyM = item.optDouble("accuracy_m", 0.0),
                        collectedAt = item.getDouble("collected_at"),
                    ),
                )
            }
        }

        private fun parseDiagnostics(items: JSONArray): List<DiagnosticStatus> = buildList {
            for (index in 0 until items.length()) {
                val item = items.getJSONObject(index)
                val diagnostic = item.optJSONObject("diagnostic") ?: JSONObject()
                add(
                    DiagnosticStatus(
                        eventId = item.getString("event_id"),
                        action = item.getString("action"),
                        category = diagnostic.optString("category", "execution"),
                        recommendation = diagnostic.optString(
                            "recommendation",
                            "review_the_redacted_device_log",
                        ),
                    ),
                )
            }
        }
    }
}

class ObserverApiException(message: String) : Exception(message)

data class ObserverSnapshot(
    val actions: List<ActionStatus>,
    val diagnostics: List<DiagnosticStatus>,
    val locations: List<LocationPoint> = emptyList(),
)

data class LocationPoint(
    val latitude: Double,
    val longitude: Double,
    val accuracyM: Double,
    val collectedAt: Double,
)

data class ActionStatus(
    val id: String,
    val action: String,
    val status: String,
    val updatedAt: Double,
)

data class DiagnosticStatus(
    val eventId: String,
    val action: String,
    val category: String,
    val recommendation: String,
)
