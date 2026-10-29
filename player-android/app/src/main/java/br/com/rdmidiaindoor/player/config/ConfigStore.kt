package br.com.rdmidiaindoor.player.config

import android.annotation.SuppressLint
import android.content.Context
import android.content.SharedPreferences
import br.com.rdmidiaindoor.player.BuildConfig
import br.com.rdmidiaindoor.player.net.UrlPolicy
import br.com.rdmidiaindoor.player.protocol.AppUpdateOffer

/**
 * Configuração local (sem domínio hardcoded): URL base da API (primeiro boot / menu técnico / provisionamento),
 * PIN técnico (hash), estado do kiosk. Gravação síncrona (`commit`) para sobreviver a queda de energia.
 */
@SuppressLint("ApplySharedPref") // commit() síncrono é intencional (queda de energia)
class ConfigStore(context: Context) {
    private val prefs: SharedPreferences = context.getSharedPreferences("rd_config", Context.MODE_PRIVATE)

    val cleartextHosts: Set<String> = BuildConfig.CLEARTEXT_HOSTS.split(',').map { it.trim() }.filter { it.isNotEmpty() }.toSet()

    var apiBaseUrl: String?
        get() = prefs.getString(K_API, null) ?: BuildConfig.DEFAULT_API_BASE_URL.takeIf { it.isNotBlank() }
        set(v) { prefs.edit().putString(K_API, v?.let { UrlPolicy.normalize(it) }).commit() }

    /** URL base do painel (para o QR quando o servidor não informar `panel_pair_url`). */
    var panelBaseUrl: String?
        get() = prefs.getString(K_PANEL, null)
        set(v) { prefs.edit().putString(K_PANEL, v?.trim()?.trimEnd('/')).commit() }

    fun validateApiUrl(url: String): String? = UrlPolicy.validate(url, cleartextHosts)

    val isConfigured: Boolean get() = apiBaseUrl?.let { validateApiUrl(it) == null } ?: false

    var techPinHash: String?
        get() = prefs.getString(K_PIN, null)
        set(v) { prefs.edit().putString(K_PIN, v).commit() }

    var pinFailures: Int
        get() = prefs.getInt(K_PIN_FAIL, 0)
        set(v) { prefs.edit().putInt(K_PIN_FAIL, v).commit() }

    var pinLockedUntilWallMs: Long
        get() = prefs.getLong(K_PIN_LOCK, 0L)
        set(v) { prefs.edit().putLong(K_PIN_LOCK, v).commit() }

    /** Kiosk suspenso pelo técnico (PIN). É reativado no próximo boot. */
    var kioskSuspended: Boolean
        get() = prefs.getBoolean(K_KIOSK_SUSP, false)
        set(v) { prefs.edit().putBoolean(K_KIOSK_SUSP, v).commit() }

    /** Abrir o player automaticamente no boot (modo APK comum; no kiosk é sempre). */
    var autostart: Boolean
        get() = prefs.getBoolean(K_AUTOSTART, true)
        set(v) { prefs.edit().putBoolean(K_AUTOSTART, v).commit() }

    var setupDone: Boolean
        get() = prefs.getBoolean(K_SETUP, false)
        set(v) { prefs.edit().putBoolean(K_SETUP, v).commit() }

    /** Chaves de release para app-update (key_id → SPKI): build (`rd.update.publicKeys`) + provisionamento. */
    var updatePublicKeys: Map<String, String>
        get() = AppUpdateOffer.parseKeys(BuildConfig.UPDATE_PUBLIC_KEYS) + AppUpdateOffer.parseKeys(prefs.getString(K_UPDATE_KEYS, "") ?: "")
        set(v) { prefs.edit().putString(K_UPDATE_KEYS, v.entries.joinToString(";") { "${it.key}=${it.value}" }).commit() }

    companion object {
        private const val K_API = "api_base_url"
        private const val K_PANEL = "panel_base_url"
        private const val K_PIN = "tech_pin_hash"
        private const val K_PIN_FAIL = "pin_failures"
        private const val K_PIN_LOCK = "pin_locked_until"
        private const val K_KIOSK_SUSP = "kiosk_suspended"
        private const val K_AUTOSTART = "autostart"
        private const val K_SETUP = "setup_done"
        private const val K_UPDATE_KEYS = "update_public_keys"

        // Extras de provisionamento (QR de Device Owner: PROVISIONING_ADMIN_EXTRAS_BUNDLE)
        const val EXTRA_API_BASE_URL = "rd_api_base_url"
        const val EXTRA_PANEL_BASE_URL = "rd_panel_base_url"
        const val EXTRA_TECH_PIN_HASH = "rd_tech_pin_hash"
        const val EXTRA_UPDATE_PUBLIC_KEYS = "rd_update_public_keys"
    }
}
