package br.com.rdmidiaindoor.player.protocol

import android.os.Build
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Log
import java.security.KeyPairGenerator
import java.security.KeyStore
import java.security.PrivateKey
import java.security.spec.ECGenParameterSpec

/**
 * Chave ES256 do dispositivo no **Android Keystore** (RDPP §1): par EC P-256 gerado no aparelho, NÃO exportável
 * (o material privado nunca sai do Keystore/TEE/StrongBox). O servidor só conhece o SPKI público.
 */
class KeystoreSigner private constructor(private val alias: String) : DeviceSigner {

    private val keyStore: KeyStore = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }

    override val publicKeySpki: ByteArray
        get() = keyStore.getCertificate(alias).publicKey.encoded

    override fun sign(data: ByteArray): ByteArray {
        val key = keyStore.getKey(alias, null) as PrivateKey
        return Es256.signRaw(key, data)
    }

    companion object {
        private const val TAG = "RdKeystore"
        const val ANDROID_KEYSTORE = "AndroidKeyStore"
        const val DEFAULT_ALIAS = "rd_device_es256_v1"

        /** Obtém a chave existente ou gera uma nova (preferindo StrongBox quando disponível). */
        fun getOrCreate(alias: String = DEFAULT_ALIAS): KeystoreSigner {
            val ks = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
            if (!ks.containsAlias(alias)) generate(alias)
            return KeystoreSigner(alias)
        }

        /** Re-pareamento após revogação: nova chave (nunca reutilizar identidade revogada — RDPP §1.2). */
        fun delete(alias: String = DEFAULT_ALIAS) {
            val ks = KeyStore.getInstance(ANDROID_KEYSTORE).apply { load(null) }
            if (ks.containsAlias(alias)) ks.deleteEntry(alias)
        }

        private fun spec(alias: String, strongBox: Boolean): KeyGenParameterSpec {
            val b = KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_SIGN or KeyProperties.PURPOSE_VERIFY)
                .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
                .setDigests(KeyProperties.DIGEST_SHA256)
                .setUserAuthenticationRequired(false)
            if (strongBox && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) b.setIsStrongBoxBacked(true)
            return b.build()
        }

        private fun generate(alias: String) {
            val gen = KeyPairGenerator.getInstance(KeyProperties.KEY_ALGORITHM_EC, ANDROID_KEYSTORE)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                try {
                    gen.initialize(spec(alias, strongBox = true))
                    gen.generateKeyPair()
                    return
                } catch (e: Exception) {
                    // StrongBoxUnavailableException (API 28+) ou falha do HAL: cai para o Keystore padrão (TEE)
                    Log.i(TAG, "StrongBox indisponível (${e.javaClass.simpleName}); usando Keystore padrão")
                }
            }
            gen.initialize(spec(alias, strongBox = false))
            gen.generateKeyPair()
        }
    }
}
