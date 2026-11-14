package ai.karry.observer

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

class SecureTokenStore(context: Context) {
    private val preferences = context.getSharedPreferences("karry_observer", Context.MODE_PRIVATE)
    private val keyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    fun saveRegistration(registration: Registration) {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val encrypted = cipher.doFinal(registration.deviceToken.toByteArray(Charsets.UTF_8))
        preferences.edit()
            .putString("server_url", registration.serverUrl)
            .putString("observer_id", registration.observerId)
            .putString("operation_id", registration.operationId)
            .putString("token_ciphertext", Base64.encodeToString(encrypted, Base64.NO_WRAP))
            .putString("token_iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .apply()
    }

    fun loadRegistration(): Registration? {
        val serverUrl = preferences.getString("server_url", null) ?: return null
        val observerId = preferences.getString("observer_id", null) ?: return null
        val operationId = preferences.getString("operation_id", null) ?: return null
        val encrypted = preferences.getString("token_ciphertext", null) ?: return null
        val iv = preferences.getString("token_iv", null) ?: return null
        return try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(
                Cipher.DECRYPT_MODE,
                key(),
                GCMParameterSpec(128, Base64.decode(iv, Base64.NO_WRAP)),
            )
            val token = cipher.doFinal(Base64.decode(encrypted, Base64.NO_WRAP))
                .toString(Charsets.UTF_8)
            Registration(serverUrl, observerId, operationId, token)
        } catch (_: Exception) {
            clear()
            null
        }
    }

    /** True when the Keystore-backed key can be created or loaded on this device. */
    fun isAvailable(): Boolean = runCatching { key() }.isSuccess

    fun clear() {
        preferences.edit().clear().apply()
    }

    private fun key(): SecretKey {
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(
                KeyGenParameterSpec.Builder(
                    KEY_ALIAS,
                    KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
                )
                    .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                    .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                    .build(),
            )
            generateKey()
        }
    }

    private companion object {
        const val KEY_ALIAS = "karry_observer_device_token"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
    }
}

data class Registration(
    val serverUrl: String,
    val observerId: String,
    val operationId: String,
    val deviceToken: String,
)
