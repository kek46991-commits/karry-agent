package ai.karry.observer

/** Raw English yes/no setup log for the observer. No onboarding screens. */
object SetupStatus {
    data class Snapshot(
        val appName: String,
        val applicationId: String,
        val serverUrlValid: Boolean,
        val ownerTokenEntered: Boolean,
        val enrolled: Boolean,
        val backendReachable: Boolean?,
        val secureStorage: Boolean,
    )

    fun lines(s: Snapshot): List<String> {
        fun yn(value: Boolean) = if (value) "yes" else "no"
        return listOf(
            "app: ${s.appName} (${s.applicationId})",
            "network_permission: yes (INTERNET, granted at install)",
            "runtime_permissions_required: no",
            "device_data_access: no (observer reads only iPhone action events)",
            "server_url: ${yn(s.serverUrlValid)}",
            "backend_reachable: " + when (s.backendReachable) {
                null -> "checking"
                true -> "yes"
                false -> "no"
            },
            "secure_storage: ${yn(s.secureStorage)} (Android Keystore)",
            "enrolled: ${yn(s.enrolled)}" + if (s.enrolled) "" else " -> enter owner token, then Yes",
            "auto_refresh: yes (2s, app in foreground)",
            "background_actions: no (observer only, foreground polling)",
            "ready: ${yn(ready(s))}",
        )
    }

    fun ready(s: Snapshot): Boolean = s.serverUrlValid && s.secureStorage && s.enrolled && s.backendReachable != false

    /** The single Yes/No question, or null when nothing needs an answer. */
    fun prompt(s: Snapshot, skipped: Boolean): String? = when {
        s.enrolled || skipped -> null
        !s.serverUrlValid -> "server_url: invalid. fix URL above (https, or http://10.0.2.2 on emulator)"
        !s.ownerTokenEntered -> "enroll: waiting for owner token above"
        else -> "enroll: register this observer now?"
    }
}
