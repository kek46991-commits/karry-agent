package ai.karry.observer

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URI
import java.net.URL

class KarryObserverApi {
    private var cachedFrameAt = 0.0
    private var cachedFrame: Bitmap? = null

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
        val screen = request(
            "${registration.serverUrl}$path/screen",
            deviceToken = registration.deviceToken,
        )
        val sharing = screen.optBoolean("sharing", false)
        val frameAt = screen.optDouble("frame_at", 0.0)
        if (sharing && frameAt > 0 && (cachedFrame == null || frameAt != cachedFrameAt)) {
            cachedFrame = requestImage(
                "${registration.serverUrl}$path/frame",
                registration.deviceToken,
            )
            cachedFrameAt = frameAt
        } else if (!sharing) {
            cachedFrameAt = 0.0
            cachedFrame = null
        }
        ObserverSnapshot(
            actions = parseActions(actions),
            diagnostics = parseDiagnostics(diagnostics),
            screen = ScreenPreview(sharing, frameAt, cachedFrame),
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

    private fun requestImage(url: String, deviceToken: String): Bitmap {
        val connection = URL(url).openConnection() as HttpURLConnection
        try {
            connection.requestMethod = "GET"
            connection.connectTimeout = 10_000
            connection.readTimeout = 10_000
            connection.setRequestProperty("Accept", "image/jpeg,image/png,image/webp")
            connection.setRequestProperty("Cache-Control", "no-store")
            connection.setRequestProperty("X-Device-Token", deviceToken)
            val status = connection.responseCode
            if (status !in 200..299) {
                val detail = connection.errorStream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }
                throw ObserverApiException(
                    runCatching { JSONObject(detail.orEmpty()).optString("detail") }.getOrNull()
                        ?.takeIf { it.isNotBlank() } ?: "HTTP $status",
                )
            }
            val data = connection.inputStream.use { it.readBytes() }
            if (data.isEmpty() || data.size > 600 * 1024) {
                throw ObserverApiException("画面画像のサイズが不正です")
            }
            return BitmapFactory.decodeByteArray(data, 0, data.size)
                ?: throw ObserverApiException("画面画像を読み取れません")
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
    val screen: ScreenPreview,
)

data class ScreenPreview(
    val sharing: Boolean,
    val frameAt: Double,
    val bitmap: Bitmap?,
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
