package br.com.rdmidiaindoor.player.ota

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageInfo
import android.content.pm.PackageInstaller
import android.content.pm.PackageManager
import android.os.Build
import android.os.SystemClock
import android.util.Log
import br.com.rdmidiaindoor.player.BuildConfig
import br.com.rdmidiaindoor.player.PlayerApp
import br.com.rdmidiaindoor.player.PlayerRuntime
import br.com.rdmidiaindoor.player.protocol.AppUpdateOffer
import br.com.rdmidiaindoor.player.protocol.Hex
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import okhttp3.Request
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.security.MessageDigest

/**
 * Atualização OTA do APK (RDPP §8; ETAPA2 #254–#258; A4 Q23).
 * 1. `GET /api/v1/player/app-update` (ondas/grupos controlados no servidor — sem atualizar todos de uma vez);
 * 2. assinatura ES256 da oferta com a CHAVE DE RELEASE fixada na config (sem chave configurada ⇒ não instala);
 * 3. download com SHA-256 e tamanho conferidos;
 * 4. APK: mesmo pacote, versionCode maior e MESMO certificado de assinatura do app instalado (PackageManager);
 * 5. instalação via PackageInstaller — silenciosa com Device Owner; sem DO o sistema pede confirmação.
 * Rollback = publicar versionCode maior com o código anterior (Android não faz downgrade).
 */
class OtaManager(private val context: Context, private val runtime: PlayerRuntime) {
    data class Result(val ok: Boolean, val detail: String)

    private val mutex = Mutex()
    @Volatile private var lastCheckElapsed = 0L

    suspend fun checkIfDue(force: Boolean): Result = mutex.withLock {
        val now = SystemClock.elapsedRealtime()
        if (!force && lastCheckElapsed != 0L && now - lastCheckElapsed < CHECK_INTERVAL_MS) return@withLock Result(true, "recente")
        lastCheckElapsed = now
        try {
            val offer = runtime.auth.withAuth { runtime.api.appUpdate(it, BuildConfig.VERSION_NAME) }
                ?: return@withLock Result(true, "sem atualização")
            install(offer)
        } catch (e: Exception) {
            Log.w(TAG, "OTA falhou: ${e.message}")
            Result(false, e.message ?: e.javaClass.simpleName)
        }
    }

    private suspend fun install(offer: AppUpdateOffer): Result {
        val keys = runtime.config.updatePublicKeys
        if (keys.isEmpty()) return Result(false, "UPDATE_KEY_NOT_CONFIGURED")
        if (!offer.verify("android", keys)) return Result(false, "UPDATE_SIGNATURE_INVALID")
        val apk = download(offer) ?: return Result(false, "DOWNLOAD_OR_SHA256_FAILED")
        val check = verifyApk(apk)
        if (check != null) {
            apk.delete()
            return Result(false, check)
        }
        return withContext(Dispatchers.IO) { installApk(apk) }
    }

    private suspend fun download(offer: AppUpdateOffer): File? = withContext(Dispatchers.IO) {
        val dir = File(context.cacheDir, "ota").apply { mkdirs() }
        val target = File(dir, "${offer.sha256}.apk")
        if (target.isFile && target.length() == offer.size && sha256(target) == offer.sha256) return@withContext target
        val url = offer.url.toHttpUrlOrNull() ?: runtime.client.url(offer.url)
        if (url.scheme != "https" && url.host !in runtime.config.cleartextHosts) return@withContext null
        val req = Request.Builder().url(url)
        // Bearer só para a própria API (URL relativa ou mesmo host); CDN externa recebe a URL sem credencial.
        val apiHost = runtime.config.apiBaseUrl?.toHttpUrlOrNull()?.host
        if (url.host == apiHost) req.header("Authorization", "Bearer ${runtime.auth.token()}")
        runtime.http.newCall(req.build()).execute().use { r ->
            if (!r.isSuccessful) return@withContext null
            val md = MessageDigest.getInstance("SHA-256")
            var total = 0L
            FileOutputStream(target).use { out ->
                val input = r.body?.byteStream() ?: return@withContext null
                val buf = ByteArray(64 * 1024)
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    total += n
                    if (total > offer.size) return@withContext null.also { target.delete() }
                    out.write(buf, 0, n)
                    md.update(buf, 0, n)
                }
            }
            if (total != offer.size || Hex.encode(md.digest()) != offer.sha256) {
                target.delete()
                return@withContext null
            }
        }
        target
    }

    /** null = APK aceito; senão o motivo da recusa. */
    @Suppress("DEPRECATION")
    private fun verifyApk(apk: File): String? {
        val pm = context.packageManager
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) PackageManager.GET_SIGNING_CERTIFICATES else PackageManager.GET_SIGNATURES
        val archive: PackageInfo = pm.getPackageArchiveInfo(apk.absolutePath, flags) ?: return "APK_INVALID"
        if (archive.packageName != context.packageName) return "APK_PACKAGE_MISMATCH"
        val installed = pm.getPackageInfo(context.packageName, flags)
        if (versionCode(archive) <= versionCode(installed)) return "APK_NOT_NEWER"
        val a = signerDigests(archive)
        val b = signerDigests(installed)
        // Em API < 28 o PackageManager pode não preencher as assinaturas do arquivo; o instalador do sistema ainda
        // recusa atualização com certificado diferente (INSTALL_FAILED_UPDATE_INCOMPATIBLE).
        if (a.isNotEmpty() && a != b) return "APK_SIGNER_MISMATCH"
        if (a.isEmpty() && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) return "APK_UNSIGNED"
        return null
    }

    @Suppress("DEPRECATION")
    private fun versionCode(p: PackageInfo): Long =
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) p.longVersionCode else p.versionCode.toLong()

    @Suppress("DEPRECATION")
    private fun signerDigests(p: PackageInfo): Set<String> {
        val sigs = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            p.signingInfo?.let { if (it.hasMultipleSigners()) it.apkContentsSigners else it.signingCertificateHistory }
        } else p.signatures
        return sigs.orEmpty().map { Hex.encode(MessageDigest.getInstance("SHA-256").digest(it.toByteArray())) }.toSet()
    }

    private fun installApk(apk: File): Result {
        val installer = context.packageManager.packageInstaller
        val params = PackageInstaller.SessionParams(PackageInstaller.SessionParams.MODE_FULL_INSTALL)
        params.setAppPackageName(context.packageName)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            params.setRequireUserAction(PackageInstaller.SessionParams.USER_ACTION_NOT_REQUIRED)
        }
        val sessionId = installer.createSession(params)
        installer.openSession(sessionId).use { session ->
            FileInputStream(apk).use { input ->
                session.openWrite("base.apk", 0, apk.length()).use { out ->
                    input.copyTo(out, 64 * 1024)
                    session.fsync(out)
                }
            }
            val flags = PendingIntent.FLAG_UPDATE_CURRENT or
                (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0)
            val pi = PendingIntent.getBroadcast(context, sessionId, Intent(context, OtaStatusReceiver::class.java), flags)
            session.commit(pi.intentSender)
        }
        val silent = (context.applicationContext as? PlayerApp)?.kiosk?.isDeviceOwner == true
        return Result(true, if (silent) "instalação silenciosa (DO) enviada" else "aguardando confirmação do usuário")
    }

    private fun sha256(f: File): String {
        val md = MessageDigest.getInstance("SHA-256")
        FileInputStream(f).use { i ->
            val buf = ByteArray(64 * 1024)
            while (true) { val n = i.read(buf); if (n < 0) break; md.update(buf, 0, n) }
        }
        return Hex.encode(md.digest())
    }

    companion object {
        private const val TAG = "RdOta"
        private const val CHECK_INTERVAL_MS = 6 * 60 * 60_000L
    }
}

/** Resultado da sessão do PackageInstaller. Sem DO: abre a confirmação do sistema (STATUS_PENDING_USER_ACTION). */
class OtaStatusReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, PackageInstaller.STATUS_FAILURE)
        when (status) {
            PackageInstaller.STATUS_PENDING_USER_ACTION -> {
                @Suppress("DEPRECATION")
                val confirm = intent.getParcelableExtra<Intent>(Intent.EXTRA_INTENT) ?: return
                confirm.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                try { context.startActivity(confirm) } catch (e: Exception) { Log.w("RdOta", "confirmação: ${e.message}") }
            }
            PackageInstaller.STATUS_SUCCESS -> Log.i("RdOta", "atualização instalada")
            else -> Log.w("RdOta", "instalação falhou: $status ${intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE)}")
        }
    }
}
