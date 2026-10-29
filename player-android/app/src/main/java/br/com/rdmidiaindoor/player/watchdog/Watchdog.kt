package br.com.rdmidiaindoor.player.watchdog

import android.app.ActivityManager
import android.app.AlarmManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.Process
import android.os.SystemClock
import android.provider.Settings
import android.util.Log
import br.com.rdmidiaindoor.player.PlayerApp
import br.com.rdmidiaindoor.player.config.ConfigStore
import br.com.rdmidiaindoor.player.kiosk.KioskManager
import br.com.rdmidiaindoor.player.player.PlayerActivity
import java.io.File
import kotlin.system.exitProcess

/**
 * Tratamento de exceção não capturada: registra o crash (para rollback por crash-loop), agenda o reinício
 * do player e encerra o processo. No modo kiosk/HOME o próprio sistema também reabre o launcher.
 */
object CrashHandler {
    private const val TAG = "RdCrash"

    fun install(app: PlayerApp) {
        val previous = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, error ->
            try {
                Log.e(TAG, "exceção não capturada em ${thread.name}", error)
                val dir = File(app.filesDir, "rd/crash").apply { mkdirs() }
                File(dir, "last.txt").writeText("${System.currentTimeMillis()}\n${Log.getStackTraceString(error)}")
                try {
                    val rt = app.runtime
                    rt.state.recordCrash(System.currentTimeMillis(), rt.active?.version)
                } catch (_: Throwable) {}
                scheduleRestart(app, 2_000L)
            } catch (_: Throwable) {
            } finally {
                if (previous != null && !LivenessFile.isKioskOrAutostart(app)) previous.uncaughtException(thread, error)
                Process.killProcess(Process.myPid())
                exitProcess(10)
            }
        }
    }

    /** Agenda abertura do player (permitida a apps em background no Android 10+ só com DO, HOME ou sobreposição). */
    fun scheduleRestart(context: Context, delayMs: Long) {
        val intent = Intent(context, PlayerActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK)
        val pi = PendingIntent.getActivity(context, 77, intent, PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val am = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        am.set(AlarmManager.ELAPSED_REALTIME, SystemClock.elapsedRealtime() + delayMs, pi)
    }

    fun restartProcess(context: Context, reason: String) {
        Log.w(TAG, "reiniciando processo: $reason")
        scheduleRestart(context, 1_500L)
        Process.killProcess(Process.myPid())
        exitProcess(0)
    }
}

/**
 * Arquivo de vivacidade escrito pelo processo principal e lido pelo watchdog (processo `:watchdog`).
 * Formato: `<wallMs>;<elapsedMs>;<foreground 0|1>;<state>`.
 */
object LivenessFile {
    fun file(context: Context) = File(context.filesDir, "rd/watchdog/alive")

    fun write(context: Context, foreground: Boolean, state: String) {
        try {
            val f = file(context)
            f.parentFile?.mkdirs()
            val tmp = File(f.parentFile, "alive.tmp")
            tmp.writeText("${System.currentTimeMillis()};${SystemClock.elapsedRealtime()};${if (foreground) 1 else 0};$state")
            tmp.renameTo(f)
        } catch (_: Exception) {}
    }

    data class Snapshot(val wallMs: Long, val elapsedMs: Long, val foreground: Boolean, val state: String)

    fun read(context: Context): Snapshot? = try {
        val p = file(context).readText().split(';')
        Snapshot(p[0].toLong(), p[1].toLong(), p[2] == "1", p.getOrElse(3) { "" })
    } catch (e: Exception) {
        null
    }

    fun isKioskOrAutostart(context: Context): Boolean =
        try { KioskManager(context).isKioskActive || ConfigStore(context).autostart } catch (_: Exception) { true }
}

/**
 * Watchdog do processo principal (thread própria): detecta (1) thread principal bloqueada (estilo ANR) e
 * (2) reprodução congelada — troca de item atrasada ou posição de vídeo parada. Ação: reinicia o motor de
 * reprodução; reincidência ⇒ reinicia o processo.
 */
class PlaybackWatchdog(private val app: PlayerApp) {
    private val main = Handler(Looper.getMainLooper())
    @Volatile private var mainTick = SystemClock.elapsedRealtime()
    @Volatile var nextSwitchDueElapsed: Long = Long.MAX_VALUE
    @Volatile var lastProgressElapsed: Long = SystemClock.elapsedRealtime()
    @Volatile var foreground: Boolean = false
    @Volatile var onFrozen: (() -> Unit)? = null
    private var lastEngineRestart = 0L
    @Volatile private var running = false

    fun start() {
        if (running) return
        running = true
        Thread({ loop() }, "rd-watchdog").apply { isDaemon = true; start() }
    }

    fun stop() {
        running = false
    }

    private fun loop() {
        while (running) {
            main.post { mainTick = SystemClock.elapsedRealtime() }
            try { Thread.sleep(5_000) } catch (_: InterruptedException) { return }
            val now = SystemClock.elapsedRealtime()
            val rt = app.runtime
            LivenessFile.write(app, foreground, rt.playerState)
            if (now - mainTick > 20_000L) {
                Log.e("RdWatchdog", "thread principal bloqueada há ${now - mainTick} ms")
                rt.frozen = true
                if (now - mainTick > 45_000L) CrashHandler.restartProcess(app, "main_thread_blocked")
                continue
            }
            if (!foreground || !rt.playbackRunning) {
                rt.frozen = false
                continue
            }
            val switchLate = now - nextSwitchDueElapsed
            val noProgress = now - lastProgressElapsed
            if (switchLate > 10_000L || noProgress > 60_000L) {
                rt.frozen = true
                if (now - lastEngineRestart < 120_000L) {
                    CrashHandler.restartProcess(app, "frozen_again")
                } else {
                    lastEngineRestart = now
                    lastProgressElapsed = now
                    nextSwitchDueElapsed = Long.MAX_VALUE
                    Log.w("RdWatchdog", "reprodução congelada (atraso=$switchLate ms, sem progresso=$noProgress ms): reiniciando motor")
                    main.post { onFrozen?.invoke() }
                }
            } else {
                rt.frozen = false
            }
        }
    }
}

/** Alarme periódico para o receptor do processo `:watchdog`. */
object WatchdogScheduler {
    private const val INTERVAL_MS = 60_000L

    fun schedule(context: Context) {
        val am = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val pi = PendingIntent.getBroadcast(
            context, 78, Intent(context, WatchdogReceiver::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        am.setInexactRepeating(AlarmManager.ELAPSED_REALTIME_WAKEUP, SystemClock.elapsedRealtime() + INTERVAL_MS, INTERVAL_MS, pi)
    }
}

/**
 * Receptor em processo separado (`:watchdog`): se o processo principal morreu ou parou de atualizar a vivacidade
 * enquanto estava em primeiro plano, mata o resto e reabre o player (kiosk/autostart). Iniciar Activity a partir
 * de background no Android 10+ exige Device Owner, HOME padrão ou permissão de sobreposição.
 */
class WatchdogReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val snap = LivenessFile.read(context) ?: return
        if (!snap.foreground) return
        if (!LivenessFile.isKioskOrAutostart(context)) return
        val stale = SystemClock.elapsedRealtime() - snap.elapsedMs > 90_000L || snap.elapsedMs > SystemClock.elapsedRealtime()
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val mainProc = am.runningAppProcesses?.firstOrNull { it.processName == context.packageName }
        if (mainProc != null && !stale) return
        Log.w("RdWatchdog", "player parado (processo=${mainProc != null}, stale=$stale): reiniciando")
        if (mainProc != null) Process.killProcess(mainProc.pid)
        val canStart = Build.VERSION.SDK_INT < Build.VERSION_CODES.Q || KioskManager(context).isDeviceOwner ||
            Settings.canDrawOverlays(context)
        if (canStart) {
            context.startActivity(
                Intent(context, PlayerActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK),
            )
        } else {
            CrashHandler.scheduleRestart(context, 1_000L)
        }
    }
}
