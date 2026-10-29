package br.com.rdmidiaindoor.player.config

import android.app.Activity
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.widget.TextView
import android.widget.Toast
import br.com.rdmidiaindoor.player.PlayerApp
import br.com.rdmidiaindoor.player.R
import br.com.rdmidiaindoor.player.kiosk.WifiActivity
import br.com.rdmidiaindoor.player.player.FormBuilder

/**
 * Primeiro boot / reconfiguração (ETAPA2 #242): conexão à internet obrigatória (Wi-Fi ou cabo), URL base da API
 * (sem domínio hardcoded; HTTPS obrigatório salvo hosts de desenvolvimento do build), criação do PIN técnico
 * (obrigatório no modo kiosk) e, no modo APK comum, permissão para abrir no boot (Android 10+).
 */
class SetupActivity : Activity() {
    private lateinit var netStatus: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val app = application as PlayerApp
        val cfg = app.runtime.config
        val pinOnly = intent.getBooleanExtra(EXTRA_PIN_ONLY, false)
        val kiosk = app.kiosk
        val f = FormBuilder(this)
        f.title(getString(if (pinOnly) R.string.setup_pin_title else R.string.setup_title))

        netStatus = f.text("")
        f.button(getString(R.string.menu_wifi)) { startActivity(Intent(this, WifiActivity::class.java)) }

        val url = if (pinOnly) null else {
            f.text(getString(R.string.setup_api_hint, cfg.cleartextHosts.joinToString().ifEmpty { "—" }))
            f.input(getString(R.string.field_api_url), cfg.apiBaseUrl, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)
        }
        val panel = if (pinOnly) null else f.input(getString(R.string.field_panel_url), cfg.panelBaseUrl, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_URI)

        val needsPin = pinOnly || (cfg.techPinHash == null && kiosk.isDeviceOwner)
        val pin1 = if (needsPin) f.input(getString(R.string.field_pin), null, InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD) else null
        val pin2 = if (needsPin) f.input(getString(R.string.field_pin_confirm), null, InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD) else null

        if (!kiosk.isDeviceOwner && Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q && !pinOnly) {
            f.text(getString(R.string.setup_overlay_hint))
            f.button(getString(R.string.setup_overlay_button)) {
                try {
                    startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
                } catch (e: Exception) {
                    Toast.makeText(this, R.string.not_available_on_device, Toast.LENGTH_LONG).show()
                }
            }
        }

        f.button(getString(R.string.setup_save)) {
            if (url != null) {
                val u = url.text.toString().trim()
                val err = cfg.validateApiUrl(u)
                if (err != null) {
                    Toast.makeText(this, err, Toast.LENGTH_LONG).show()
                    return@button
                }
                if (u != cfg.apiBaseUrl) app.runtime.changeApiBaseUrl(u)
                panel?.text?.toString()?.trim()?.let { cfg.panelBaseUrl = it.ifBlank { null } }
            }
            if (pin1 != null && pin2 != null) {
                val a = pin1.text.toString()
                if (!PinHasher.isAcceptablePin(a) || a != pin2.text.toString()) {
                    Toast.makeText(this, R.string.pin_invalid, Toast.LENGTH_LONG).show()
                    return@button
                }
                // PBKDF2 (210 000 iterações) fora da thread principal
                Thread {
                    val h = PinHasher.hash(a)
                    runOnUiThread {
                        cfg.techPinHash = h
                        finishSetup(cfg)
                    }
                }.start()
                return@button
            }
            finishSetup(cfg)
        }
        f.show()
    }

    private fun finishSetup(cfg: ConfigStore) {
        if (!isOnline()) Toast.makeText(this, R.string.setup_offline_warning, Toast.LENGTH_LONG).show()
        cfg.setupDone = true
        (application as PlayerApp).runtime.evaluatePhase()
        finish()
    }

    override fun onResume() {
        super.onResume()
        netStatus.text = getString(if (isOnline()) R.string.net_online else R.string.net_offline)
    }

    private fun isOnline(): Boolean {
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
        val caps = cm.getNetworkCapabilities(cm.activeNetwork) ?: return false
        return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
    }

    companion object {
        const val EXTRA_PIN_ONLY = "pin_only"
    }
}
