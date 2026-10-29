package br.com.rdmidiaindoor.player.kiosk

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.ActivityManager
import android.app.admin.DeviceAdminReceiver
import android.app.admin.DevicePolicyManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.PersistableBundle
import android.os.UserManager
import android.provider.Settings
import android.util.Log
import br.com.rdmidiaindoor.player.BuildConfig
import br.com.rdmidiaindoor.player.PlayerApp
import br.com.rdmidiaindoor.player.config.ConfigStore
import br.com.rdmidiaindoor.player.config.PinHasher
import br.com.rdmidiaindoor.player.player.PlayerActivity

/** Componente de administração do dispositivo (necessário para Device Owner). */
class RdDeviceAdminReceiver : DeviceAdminReceiver() {
    override fun onProfileProvisioningComplete(context: Context, intent: Intent) {
        // Provisionamento por QR: extras opcionais (URL da API, hash do PIN técnico, chaves de release).
        @Suppress("DEPRECATION")
        val extras: PersistableBundle? = intent.getParcelableExtra(DevicePolicyManager.EXTRA_PROVISIONING_ADMIN_EXTRAS_BUNDLE)
        KioskManager.applyProvisioningExtras(context, extras)
        KioskManager(context).applyPolicies()
    }

    override fun onEnabled(context: Context, intent: Intent) {
        Log.i("RdKiosk", "device admin habilitado")
    }

    companion object {
        fun component(context: Context) = ComponentName(context, RdDeviceAdminReceiver::class.java)
    }
}

/**
 * Modo kiosk/launcher (ADR_008 item 3; ETAPA2 #239–#253; P001 §9).
 * - COM Device Owner: app vira HOME persistente, Lock Task sem barra/home/recentes, restrições de usuário
 *   (instalação, USB, Bluetooth, modo seguro, reset de fábrica, depuração em release…), apps de loja/vídeo/navegador
 *   ocultados (reversível), keyguard e barra de status desativados, tela sempre ligada na energia, localização
 *   concedida para ler o SSID. Wi-Fi permanece configurável pela tela própria do app.
 * - SEM Device Owner: "APK comum" — nenhum bloqueio (ETAPA2 #246).
 */
class KioskManager(private val context: Context) {
    private val dpm = context.getSystemService(Context.DEVICE_POLICY_SERVICE) as DevicePolicyManager
    private val admin = RdDeviceAdminReceiver.component(context)
    private val config by lazy { ConfigStore(context) }

    val isDeviceOwner: Boolean get() = dpm.isDeviceOwnerApp(context.packageName)

    /** Kiosk ativo = Device Owner e não suspenso pelo técnico. */
    val isKioskActive: Boolean get() = isDeviceOwner && !config.kioskSuspended

    fun mode(): String = when {
        !isDeviceOwner -> "APK_COMUM"
        config.kioskSuspended -> "KIOSK_SUSPENSO"
        else -> "KIOSK"
    }

    private val restrictions: List<String>
        get() {
            val list = mutableListOf(
                UserManager.DISALLOW_INSTALL_APPS,
                UserManager.DISALLOW_INSTALL_UNKNOWN_SOURCES,
                UserManager.DISALLOW_UNINSTALL_APPS,
                UserManager.DISALLOW_USB_FILE_TRANSFER,
                UserManager.DISALLOW_MOUNT_PHYSICAL_MEDIA,
                UserManager.DISALLOW_CONFIG_BLUETOOTH,
                UserManager.DISALLOW_SAFE_BOOT,
                UserManager.DISALLOW_FACTORY_RESET,
                UserManager.DISALLOW_ADD_USER,
                UserManager.DISALLOW_MODIFY_ACCOUNTS,
                UserManager.DISALLOW_CONFIG_CREDENTIALS,
                UserManager.DISALLOW_OUTGOING_BEAM,
                UserManager.DISALLOW_CREATE_WINDOWS,
            )
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) list += UserManager.DISALLOW_BLUETOOTH
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                list += UserManager.DISALLOW_SYSTEM_ERROR_DIALOGS
                list += UserManager.DISALLOW_AIRPLANE_MODE
            }
            // Em release a depuração USB/adb também é bloqueada; em debug fica liberada para o técnico/desenvolvedor.
            if (BuildConfig.KIOSK_BLOCK_ADB) list += UserManager.DISALLOW_DEBUGGING_FEATURES
            return list
        }

    /** Aplica todas as políticas (idempotente). Sem Device Owner: não faz nada. */
    fun applyPolicies(): Boolean {
        if (!isDeviceOwner) return false
        try {
            dpm.setLockTaskPackages(admin, arrayOf(context.packageName))
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                dpm.setLockTaskFeatures(admin, DevicePolicyManager.LOCK_TASK_FEATURE_NONE)
            }
            for (r in restrictions) dpm.addUserRestriction(admin, r)
            for (pkg in BLOCKED_PACKAGES) {
                try { dpm.setApplicationHidden(admin, pkg, true) } catch (_: Exception) {}
            }
            dpm.setKeyguardDisabled(admin, true)
            dpm.setStatusBarDisabled(admin, true)
            dpm.setGlobalSetting(admin, Settings.Global.STAY_ON_WHILE_PLUGGED_IN, "7")
            setHomeAliasEnabled(true)
            val home = IntentFilter(Intent.ACTION_MAIN).apply {
                addCategory(Intent.CATEGORY_HOME)
                addCategory(Intent.CATEGORY_DEFAULT)
            }
            dpm.addPersistentPreferredActivity(admin, home, ComponentName(context, PlayerActivity::class.java))
            grantLocation()
            return true
        } catch (e: SecurityException) {
            Log.w(TAG, "falha ao aplicar políticas: ${e.message}")
            return false
        }
    }

    private fun grantLocation() {
        for (perm in listOf(Manifest.permission.ACCESS_FINE_LOCATION, Manifest.permission.ACCESS_COARSE_LOCATION)) {
            try {
                dpm.setPermissionGrantState(admin, context.packageName, perm, DevicePolicyManager.PERMISSION_GRANT_STATE_GRANTED)
            } catch (_: Exception) {}
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            try { dpm.setLocationEnabled(admin, true) } catch (_: Exception) {}
        }
    }

    /** Saída do kiosk pelo técnico (PIN já validado). Reverte bloqueios até o próximo boot. */
    fun suspendKiosk(activity: Activity?) {
        config.kioskSuspended = true
        activity?.let { try { it.stopLockTask() } catch (_: Exception) {} }
        if (!isDeviceOwner) return
        for (r in restrictions) try { dpm.clearUserRestriction(admin, r) } catch (_: Exception) {}
        for (pkg in BLOCKED_PACKAGES) try { dpm.setApplicationHidden(admin, pkg, false) } catch (_: Exception) {}
        try { dpm.setStatusBarDisabled(admin, false) } catch (_: Exception) {}
        try { dpm.setKeyguardDisabled(admin, false) } catch (_: Exception) {}
        try { dpm.clearPackagePersistentPreferredActivities(admin, context.packageName) } catch (_: Exception) {}
    }

    fun resumeKiosk(activity: Activity?) {
        config.kioskSuspended = false
        applyPolicies()
        activity?.let { enterLockTaskIfNeeded(it) }
    }

    fun enterLockTaskIfNeeded(activity: Activity) {
        if (!isKioskActive) return
        if (!dpm.isLockTaskPermitted(context.packageName)) applyPolicies()
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val inLockTask = am.lockTaskModeState != ActivityManager.LOCK_TASK_MODE_NONE
        if (!inLockTask) try { activity.startLockTask() } catch (e: Exception) { Log.w(TAG, "startLockTask: ${e.message}") }
    }

    /** Reinício do aparelho (comando `reboot`) — só com Device Owner (API 24+). */
    fun reboot(): Boolean {
        if (!isDeviceOwner) return false
        return try { dpm.reboot(admin); true } catch (e: Exception) { false }
    }

    /** HOME via activity-alias: habilitado só no kiosk ou quando o técnico define o player como tela inicial. */
    fun setHomeAliasEnabled(enabled: Boolean) {
        val alias = ComponentName(context, "${context.packageName}.HomeAlias")
        context.packageManager.setComponentEnabledSetting(
            alias,
            if (enabled) PackageManager.COMPONENT_ENABLED_STATE_ENABLED else PackageManager.COMPONENT_ENABLED_STATE_DISABLED,
            PackageManager.DONT_KILL_APP,
        )
    }

    /** Remove o Device Owner (descomissionamento pelo técnico, com PIN). Irreversível sem novo provisionamento. */
    @SuppressLint("MissingPermission")
    @Suppress("DEPRECATION")
    fun releaseDeviceOwner(activity: Activity?) {
        suspendKiosk(activity)
        setHomeAliasEnabled(false)
        if (isDeviceOwner) try { dpm.clearDeviceOwnerApp(context.packageName) } catch (e: Exception) { Log.w(TAG, "clearDeviceOwnerApp: ${e.message}") }
    }

    companion object {
        private const val TAG = "RdKiosk"

        /** Ocultados no kiosk (reversível). Lista conhecida; fabricantes podem ter outros pacotes. */
        val BLOCKED_PACKAGES = listOf(
            "com.android.vending",
            "com.google.android.youtube",
            "com.google.android.youtube.tv",
            "com.google.android.youtube.tvkids",
            "com.android.chrome",
            "com.android.browser",
            "org.chromium.webview_shell",
            "com.google.android.apps.tv.launcherx",
            "com.google.android.tvlauncher",
            "com.amazon.venezia",
        )

        fun applyProvisioningExtras(context: Context, extras: PersistableBundle?) {
            extras ?: return
            val cfg = ConfigStore(context)
            extras.getString(ConfigStore.EXTRA_API_BASE_URL)?.let { if (cfg.validateApiUrl(it) == null) cfg.apiBaseUrl = it }
            extras.getString(ConfigStore.EXTRA_PANEL_BASE_URL)?.let { cfg.panelBaseUrl = it }
            extras.getString(ConfigStore.EXTRA_TECH_PIN_HASH)?.let { if (PinHasher.isValidEncoded(it)) cfg.techPinHash = it }
            extras.getString(ConfigStore.EXTRA_UPDATE_PUBLIC_KEYS)?.let {
                cfg.updatePublicKeys = br.com.rdmidiaindoor.player.protocol.AppUpdateOffer.parseKeys(it)
            }
        }

        fun get(context: Context): KioskManager = (context.applicationContext as? PlayerApp)?.kiosk ?: KioskManager(context)
    }
}
