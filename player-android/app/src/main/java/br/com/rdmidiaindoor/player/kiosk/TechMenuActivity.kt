package br.com.rdmidiaindoor.player.kiosk

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Toast
import br.com.rdmidiaindoor.player.BuildConfig
import br.com.rdmidiaindoor.player.PlayerApp
import br.com.rdmidiaindoor.player.R
import br.com.rdmidiaindoor.player.config.SetupActivity
import br.com.rdmidiaindoor.player.player.FormBuilder
import br.com.rdmidiaindoor.player.watchdog.CrashHandler
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Menu técnico (aberto somente após o PIN — ETAPA2 #243–#244, #249): diagnóstico, URL da API, re-pareamento,
 * ressincronização, alteração de PIN, sair/reativar kiosk, configurações do sistema (só com kiosk suspenso),
 * reinício do app/aparelho e, no modo APK comum, definir como tela inicial e permitir abrir no boot.
 */
class TechMenuActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        render()
    }

    private fun render() {
        val app = application as PlayerApp
        val rt = app.runtime
        val kiosk = app.kiosk
        val id = rt.state.identity
        val p = rt.genStore.pointer()
        val f = FormBuilder(this)
        f.title(getString(R.string.tech_title))
        f.text(
            getString(
                R.string.tech_diag,
                BuildConfig.VERSION_NAME, BuildConfig.VERSION_CODE, kiosk.mode(),
                id?.deviceId ?: "—", id?.screenCode ?: "—",
                rt.config.apiBaseUrl ?: "—",
                p.active?.toString() ?: "—", p.staged?.toString() ?: "—", p.previous?.toString() ?: "—",
                rt.playerState, if (rt.isOnline()) "online" else "offline",
                rt.clock.offsetMs, rt.alerts().joinToString().ifEmpty { "—" },
            ),
            size = 15f,
        )
        f.button(getString(R.string.menu_wifi)) { startActivity(Intent(this, WifiActivity::class.java)) }
        f.button(getString(R.string.tech_resync)) {
            rt.requestSync("tech_menu", force = true)
            Toast.makeText(this, R.string.tech_resync_started, Toast.LENGTH_SHORT).show()
        }
        f.button(getString(R.string.tech_outbox)) {
            rt.scope.launch {
                val n = try { rt.popStore.count() } catch (e: Exception) { -1 }
                rt.flushEvents()
                val after = try { rt.popStore.count() } catch (e: Exception) { -1 }
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@TechMenuActivity, getString(R.string.tech_outbox_result, n, after), Toast.LENGTH_LONG).show()
                }
            }
        }
        f.button(getString(R.string.tech_config)) { startActivity(Intent(this, SetupActivity::class.java)) }
        f.button(getString(R.string.tech_change_pin)) {
            startActivity(Intent(this, SetupActivity::class.java).putExtra(SetupActivity.EXTRA_PIN_ONLY, true))
        }
        f.button(getString(R.string.tech_repair)) {
            confirm(getString(R.string.tech_repair_confirm)) { rt.onDeviceRevoked(); finish() }
        }
        if (kiosk.isDeviceOwner) {
            if (kiosk.isKioskActive) {
                f.button(getString(R.string.tech_exit_kiosk)) {
                    confirm(getString(R.string.tech_exit_kiosk_confirm)) {
                        kiosk.suspendKiosk(this)
                        render()
                    }
                }
            } else {
                f.button(getString(R.string.tech_resume_kiosk)) {
                    kiosk.resumeKiosk(this)
                    render()
                }
                f.button(getString(R.string.tech_release_do)) {
                    confirm(getString(R.string.tech_release_do_confirm)) { kiosk.releaseDeviceOwner(this); render() }
                }
            }
            f.button(getString(R.string.tech_reboot)) { confirm(getString(R.string.tech_reboot_confirm)) { kiosk.reboot() } }
        } else {
            f.button(getString(R.string.tech_set_home)) {
                kiosk.setHomeAliasEnabled(true)
                try { startActivity(Intent(Settings.ACTION_HOME_SETTINGS)) } catch (e: Exception) {
                    Toast.makeText(this, R.string.not_available_on_device, Toast.LENGTH_LONG).show()
                }
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                f.button(getString(R.string.setup_overlay_button)) {
                    try {
                        startActivity(Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION, Uri.parse("package:$packageName")))
                    } catch (e: Exception) {
                        Toast.makeText(this, R.string.not_available_on_device, Toast.LENGTH_LONG).show()
                    }
                }
            }
        }
        if (!kiosk.isKioskActive) {
            f.button(getString(R.string.tech_system_settings)) {
                try { startActivity(Intent(Settings.ACTION_SETTINGS)) } catch (_: Exception) {}
            }
        }
        f.button(getString(R.string.tech_restart_app)) { CrashHandler.restartProcess(this, "tech_menu") }
        f.button(getString(R.string.back)) { finish() }
        f.show()
    }

    private fun confirm(message: String, action: () -> Unit) {
        AlertDialog.Builder(this)
            .setMessage(message)
            .setPositiveButton(R.string.ok) { _, _ -> action() }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }
}
