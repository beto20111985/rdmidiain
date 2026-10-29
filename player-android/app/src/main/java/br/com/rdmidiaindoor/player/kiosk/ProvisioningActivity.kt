package br.com.rdmidiaindoor.player.kiosk

import android.app.Activity
import android.app.admin.DevicePolicyManager
import android.content.Intent
import android.os.Bundle
import android.os.PersistableBundle

/**
 * Provisionamento de Device Owner por QR (Android 10+ exige que o DPC trate
 * `GET_PROVISIONING_MODE` e `ADMIN_POLICY_COMPLIANCE`). Escolhe "dispositivo totalmente gerenciado" e aplica as
 * políticas do kiosk. Provisionamento por QR depende do suporte do fabricante (muitas TV Box genéricas não têm).
 */
class ProvisioningActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        when (intent?.action) {
            ACTION_GET_PROVISIONING_MODE -> {
                @Suppress("DEPRECATION")
                val extras: PersistableBundle? = intent.getParcelableExtra(DevicePolicyManager.EXTRA_PROVISIONING_ADMIN_EXTRAS_BUNDLE)
                KioskManager.applyProvisioningExtras(this, extras)
                val result = Intent().putExtra(EXTRA_PROVISIONING_MODE, PROVISIONING_MODE_FULLY_MANAGED_DEVICE)
                setResult(RESULT_OK, result)
            }
            ACTION_ADMIN_POLICY_COMPLIANCE -> {
                @Suppress("DEPRECATION")
                val extras: PersistableBundle? = intent.getParcelableExtra(DevicePolicyManager.EXTRA_PROVISIONING_ADMIN_EXTRAS_BUNDLE)
                KioskManager.applyProvisioningExtras(this, extras)
                KioskManager(this).applyPolicies()
                setResult(RESULT_OK)
            }
            else -> setResult(RESULT_CANCELED)
        }
        finish()
    }

    companion object {
        // Constantes da API 29+ (literais para compilar/rodar desde a API 24)
        const val ACTION_GET_PROVISIONING_MODE = "android.app.action.GET_PROVISIONING_MODE"
        const val ACTION_ADMIN_POLICY_COMPLIANCE = "android.app.action.ADMIN_POLICY_COMPLIANCE"
        const val EXTRA_PROVISIONING_MODE = "android.app.extra.PROVISIONING_MODE"
        const val PROVISIONING_MODE_FULLY_MANAGED_DEVICE = 1
    }
}
