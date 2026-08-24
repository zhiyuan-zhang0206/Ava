package com.ava.app

import android.content.Context
import android.util.Base64
import java.nio.charset.StandardCharsets
import java.security.GeneralSecurityException
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties

/**
 * Stores the optional cluster secret outside WebView and settings.json.
 *
 * Android Keystore owns the AES key; SharedPreferences holds only AES-GCM
 * ciphertext and its nonce, both unusable without the non-exportable key.
 */
class AvaSecretStore(context: Context) {
    private val preferences = context.getSharedPreferences(PREFERENCES, Context.MODE_PRIVATE)

    @Throws(GeneralSecurityException::class)
    fun save(secret: String) {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        val ciphertext = cipher.doFinal(secret.toByteArray(StandardCharsets.UTF_8))
        if (!preferences.edit()
                .putString(CIPHERTEXT, Base64.encodeToString(ciphertext, Base64.NO_WRAP))
                .putString(NONCE, Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
                .commit()) {
            throw GeneralSecurityException("could not persist encrypted cluster secret")
        }
    }

    fun get(): String? {
        val ciphertext = preferences.getString(CIPHERTEXT, null) ?: return null
        val nonce = preferences.getString(NONCE, null) ?: return null
        return try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(
                Cipher.DECRYPT_MODE,
                key(),
                GCMParameterSpec(TAG_LENGTH_BITS, Base64.decode(nonce, Base64.NO_WRAP)),
            )
            String(cipher.doFinal(Base64.decode(ciphertext, Base64.NO_WRAP)), StandardCharsets.UTF_8)
        } catch (_: GeneralSecurityException) {
            clear()
            null
        } catch (_: IllegalArgumentException) {
            clear()
            null
        }
    }

    fun clear() {
        preferences.edit().remove(CIPHERTEXT).remove(NONCE).apply()
    }

    @Throws(GeneralSecurityException::class)
    private fun key(): SecretKey {
        val keyStore = KeyStore.getInstance(KEYSTORE).apply { load(null) }
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }

        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, KEYSTORE)
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .build(),
        )
        return generator.generateKey()
    }

    private companion object {
        const val PREFERENCES = "ava_secret_store"
        const val CIPHERTEXT = "ciphertext"
        const val NONCE = "nonce"
        const val KEYSTORE = "AndroidKeyStore"
        const val KEY_ALIAS = "ava_cluster_secret"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val TAG_LENGTH_BITS = 128
    }
}
