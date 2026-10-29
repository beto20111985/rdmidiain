package br.com.rdmidiaindoor.player

import android.app.Application
import android.os.Build
import br.com.rdmidiaindoor.player.kiosk.KioskManager
import br.com.rdmidiaindoor.player.watchdog.CrashHandler
import br.com.rdmidiaindoor.player.watchdog.WatchdogScheduler
import java.io.File

/**
 * Application do player. O processo `:watchdog` (receptor de alarme isolado) NÃO inicializa o runtime.
 */
class PlayerApp : Application() {

    lateinit var runtime: PlayerRuntime
        private set

    val kiosk: KioskManager by lazy { KioskManager(this) }

    val isWatchdogProcess: Boolean by lazy { currentProcessName().endsWith(":watchdog") }

    override fun onCreate() {
        super.onCreate()
        if (isWatchdogProcess) return
        // Direct Boot: o armazenamento credential-encrypted só existe após o desbloqueio do usuário
        val um = getSystemService(USER_SERVICE) as android.os.UserManager
        if (!um.isUserUnlocked) return
        CrashHandler.install(this)
        runtime = PlayerRuntime(this)
        runtime.start()
        WatchdogScheduler.schedule(this)
    }

    private fun currentProcessName(): String {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) return getProcessName()
        return try {
            File("/proc/self/cmdline").readText().trim { it <= ' ' || it == '\u0000' }
        } catch (e: Exception) {
            packageName
        }
    }
}
