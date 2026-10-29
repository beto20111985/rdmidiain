package br.com.rdmidiaindoor.player.boot

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.provider.Settings
import android.util.Log
import br.com.rdmidiaindoor.player.config.ConfigStore
import br.com.rdmidiaindoor.player.kiosk.KioskManager
import br.com.rdmidiaindoor.player.player.PlayerActivity
import br.com.rdmidiaindoor.player.watchdog.WatchdogScheduler

/**
 * Boot automático (ETAPA2 #250–#253).
 * - `LOCKED_BOOT_COMPLETED`: tratado por [LockedBootReceiver] (directBootAware, processo `:watchdog`), pois o
 *   armazenamento credential-encrypted ainda está trancado; TV Box sem tela de bloqueio desbloqueia em seguida.
 * - `BOOT_COMPLETED` / `MY_PACKAGE_REPLACED` (após OTA) / `QUICKBOOT_POWERON` (alguns fabricantes): reativa o kiosk
 *   suspenso, reaplica políticas, agenda o watchdog e abre o player.
 * Android 10+: abrir Activity a partir do background só é permitido se o app for Device Owner, HOME padrão
 * (o sistema já abre o launcher) ou tiver a permissão "sobrepor a outros apps" (SYSTEM_ALERT_WINDOW).
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        Log.i(TAG, "boot: $action")
        val cfg = ConfigStore(context)
        if (action == Intent.ACTION_BOOT_COMPLETED || action == ACTION_QUICKBOOT) cfg.kioskSuspended = false
        val kiosk = KioskManager(context)
        if (kiosk.isDeviceOwner) kiosk.applyPolicies()
        WatchdogScheduler.schedule(context)
        if (!kiosk.isKioskActive && !cfg.autostart) return
        val allowed = Build.VERSION.SDK_INT < Build.VERSION_CODES.Q || kiosk.isDeviceOwner || Settings.canDrawOverlays(context)
        if (!allowed) {
            Log.w(TAG, "Android 10+: sem DO/HOME/sobreposição — o player não pode se abrir sozinho no boot")
            return
        }
        try {
            context.startActivity(Intent(context, PlayerActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        } catch (e: Exception) {
            Log.w(TAG, "falha ao abrir player no boot: ${e.message}")
        }
    }

    companion object {
        private const val TAG = "RdBoot"
        const val ACTION_QUICKBOOT = "android.intent.action.QUICKBOOT_POWERON"
    }
}

/**
 * `LOCKED_BOOT_COMPLETED` (Direct Boot): roda no processo `:watchdog`, que não toca no armazenamento CE.
 * Apenas agenda o watchdog; o player abre no `BOOT_COMPLETED` após o desbloqueio do usuário.
 */
class LockedBootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_LOCKED_BOOT_COMPLETED) return
        Log.i("RdBoot", "locked boot: aguardando desbloqueio do usuário")
        WatchdogScheduler.schedule(context)
    }
}
