package br.com.rdmidiaindoor.player.net

import android.annotation.SuppressLint
import android.app.ActivityManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.res.Configuration
import android.media.AudioManager
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Process
import android.os.StatFs
import android.os.SystemClock
import android.util.DisplayMetrics
import android.view.WindowManager
import androidx.core.content.ContextCompat
import br.com.rdmidiaindoor.player.BuildConfig
import br.com.rdmidiaindoor.player.protocol.PlayerMessages

/**
 * Telemetria de sistema (RDPP §4; ETAPA2 #306–#318). Nunca coleta IP.
 * - CPU: desde o Android 8 o `/proc/stat` global é bloqueado por SELinux para apps; reportamos o uso de CPU do
 *   PRÓPRIO processo (tempo de CPU / tempo decorrido / núcleos) — limitação documentada.
 * - SSID exige permissão de localização (concedida automaticamente pelo Device Owner) e, no Android 8.1+, localização
 *   ligada; sem isso o SSID é `null` e só o RSSI é informado.
 * - HDMI: broadcast fixo `HDMI_PLUGGED`/`ACTION_HDMI_AUDIO_PLUG` quando o fabricante o publica; senão `null`.
 */
class TelemetryCollector(private val context: Context) {
    private var lastCpuMs = Process.getElapsedCpuTime()
    private var lastWallMs = SystemClock.elapsedRealtime()

    @Synchronized
    fun cpuPct(): Long? {
        val cpu = Process.getElapsedCpuTime()
        val wall = SystemClock.elapsedRealtime()
        val dCpu = cpu - lastCpuMs
        val dWall = wall - lastWallMs
        lastCpuMs = cpu
        lastWallMs = wall
        if (dWall <= 0) return null
        val cores = Runtime.getRuntime().availableProcessors().coerceAtLeast(1)
        return (dCpu * 100 / (dWall * cores)).coerceIn(0, 100)
    }

    fun collect(appStorageDir: java.io.File): PlayerMessages.Telemetry {
        val am = context.getSystemService(Context.ACTIVITY_SERVICE) as ActivityManager
        val mi = ActivityManager.MemoryInfo().also { am.getMemoryInfo(it) }
        val stat = StatFs(appStorageDir.absolutePath)
        val (w, h) = resolution()
        val (ssid, rssi) = wifi()
        return PlayerMessages.Telemetry(
            cpuPct = cpuPct(),
            ramUsedMb = (mi.totalMem - mi.availMem) / MB,
            ramTotalMb = mi.totalMem / MB,
            diskFreeMb = stat.availableBytes / MB,
            diskTotalMb = stat.totalBytes / MB,
            uptimeS = SystemClock.elapsedRealtime() / 1000,
            os = "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})",
            appVersion = BuildConfig.VERSION_NAME,
            platform = "android",
            resolution = "${w}x$h",
            orientation = if (context.resources.configuration.orientation == Configuration.ORIENTATION_PORTRAIT) "PORTRAIT" else "LANDSCAPE",
            wifiSsid = ssid,
            wifiRssiDbm = rssi,
            hdmiConnected = hdmi(),
        )
    }

    @Suppress("DEPRECATION")
    fun resolution(): Pair<Int, Int> {
        val wm = context.getSystemService(Context.WINDOW_SERVICE) as WindowManager
        val dm = DisplayMetrics()
        wm.defaultDisplay.getRealMetrics(dm)
        return dm.widthPixels to dm.heightPixels
    }

    @SuppressLint("MissingPermission")
    @Suppress("DEPRECATION")
    private fun wifi(): Pair<String?, Long?> = try {
        val wm = context.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val info = wm.connectionInfo
        if (info == null || info.networkId == -1) {
            null to null
        } else {
            val raw = info.ssid
            val ssid = raw?.takeIf { it != "<unknown ssid>" && it.isNotBlank() }?.removeSurrounding("\"")
            ssid to info.rssi.toLong()
        }
    } catch (e: Exception) {
        null to null
    }

    private fun hdmi(): Boolean? {
        return try {
            val sticky = ContextCompat.registerReceiver(context, null, IntentFilter("android.intent.action.HDMI_PLUGGED"), ContextCompat.RECEIVER_EXPORTED)
            if (sticky != null && sticky.hasExtra("state")) return sticky.getBooleanExtra("state", false)
            val audio: Intent? = ContextCompat.registerReceiver(context, null, IntentFilter(AudioManager.ACTION_HDMI_AUDIO_PLUG), ContextCompat.RECEIVER_EXPORTED)
            if (audio != null && audio.hasExtra(AudioManager.EXTRA_AUDIO_PLUG_STATE)) {
                audio.getIntExtra(AudioManager.EXTRA_AUDIO_PLUG_STATE, 0) == 1
            } else null
        } catch (e: Exception) {
            null
        }
    }

    companion object {
        private const val MB = 1024L * 1024L
    }
}
