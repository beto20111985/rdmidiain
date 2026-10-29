package br.com.rdmidiaindoor.player.player

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Color
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.text.InputType
import android.view.Gravity
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.Toast
import br.com.rdmidiaindoor.player.PlayerApp
import br.com.rdmidiaindoor.player.PlayerRuntime
import br.com.rdmidiaindoor.player.R
import br.com.rdmidiaindoor.player.config.PinHasher
import br.com.rdmidiaindoor.player.config.SetupActivity
import br.com.rdmidiaindoor.player.core.OfflineAlerts
import br.com.rdmidiaindoor.player.kiosk.TechMenuActivity
import br.com.rdmidiaindoor.player.kiosk.WifiActivity
import br.com.rdmidiaindoor.player.watchdog.PlaybackWatchdog

/**
 * Activity principal: player em tela cheia e, no modo kiosk, o launcher (HOME via activity-alias).
 * Controle remoto: botão de configuração/menu/OK abre opções — Wi-Fi SEM PIN (cliente) e menu técnico COM PIN
 * (ETAPA2 #245, #249). Voltar é ignorado no kiosk.
 */
class PlayerActivity : Activity(), PlayerRuntime.Ui {

    private lateinit var app: PlayerApp
    private lateinit var runtime: PlayerRuntime
    private lateinit var rotatable: RotatableContainer
    private lateinit var content: FrameLayout
    private lateinit var stageContainer: FrameLayout
    private lateinit var footer: FooterView
    private lateinit var centerAlert: CenterAlert
    private lateinit var pairingOverlay: PairingOverlay
    private lateinit var stage: Stage
    private lateinit var controller: PlayerController
    private lateinit var watchdog: PlaybackWatchdog
    private val handler = Handler(Looper.getMainLooper())
    private var visible = false
    private var setupLaunched = false

    private val uiTick = object : Runnable {
        override fun run() {
            refreshAlerts()
            handler.postDelayed(this, 15_000L)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        app = application as PlayerApp
        runtime = app.runtime
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        hideSystemUi()

        rotatable = RotatableContainer(this).apply { setBackgroundColor(Color.BLACK) }
        content = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        stageContainer = FrameLayout(this).apply { setBackgroundColor(Color.BLACK) }
        footer = FooterView(this)
        centerAlert = CenterAlert(this)
        pairingOverlay = PairingOverlay(this).apply { visibility = View.GONE }
        content.addView(stageContainer, FrameLayout.LayoutParams(-1, -1))
        content.addView(footer, FrameLayout.LayoutParams(-1, 0, Gravity.BOTTOM))
        content.addView(centerAlert, FrameLayout.LayoutParams(-2, -2, Gravity.CENTER))
        content.addView(pairingOverlay, FrameLayout.LayoutParams(-1, -1))
        rotatable.addView(content)
        setContentView(rotatable)

        stage = Stage(this, stageContainer)
        watchdog = PlaybackWatchdog(app).also { it.start() }
        controller = PlayerController(runtime, stage, { label, tz ->
            footer.setLabel(label)
            footer.tickClock(runtime.clock.now(), tz)
        }, watchdog)
        content.addOnLayoutChangeListener { v, l, t, r, b, _, _, _, _ -> v.post { applyLayout(r - l, b - t) } }
        runtime.ui = this
    }

    /** Rodapé com altura `height_permille`/1000 e área útil do anúncio reduzida (letterbox, nunca esticar). */
    private fun applyLayout(w: Int, h: Int) {
        val settings = runtime.active?.manifest?.settings
        rotatable.portrait = settings?.orientation == "PORTRAIT"
        val permille = if (settings?.footer?.enabled == false) 0 else (settings?.footer?.heightPermille ?: 60)
        val footerH = h * permille / 1000
        val flp = footer.layoutParams as FrameLayout.LayoutParams
        if (flp.height != footerH) {
            flp.height = footerH
            footer.layoutParams = flp
            footer.applyHeight(footerH)
            footer.visibility = if (footerH > 0) View.VISIBLE else View.GONE
        }
        val slp = stageContainer.layoutParams as FrameLayout.LayoutParams
        if (slp.bottomMargin != footerH) {
            slp.bottomMargin = footerH
            stageContainer.layoutParams = slp
        }
        controller.setStageSize(w, h - footerH)
    }

    override fun onStart() {
        super.onStart()
        visible = true
        watchdog.foreground = true
        onRuntimeChanged()
        handler.post(uiTick)
    }

    override fun onResume() {
        super.onResume()
        hideSystemUi()
        app.kiosk.enterLockTaskIfNeeded(this)
    }

    override fun onStop() {
        super.onStop()
        visible = false
        watchdog.foreground = false
        handler.removeCallbacks(uiTick)
        controller.stop()
    }

    override fun onDestroy() {
        if (runtime.ui === this) runtime.ui = null
        watchdog.stop()
        stage.releaseAll()
        super.onDestroy()
    }

    override fun onWindowFocusChanged(hasFocus: Boolean) {
        super.onWindowFocusChanged(hasFocus)
        if (hasFocus) hideSystemUi()
    }

    @Suppress("DEPRECATION")
    private fun hideSystemUi() {
        window.decorView.systemUiVisibility = (View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY or View.SYSTEM_UI_FLAG_FULLSCREEN or
            View.SYSTEM_UI_FLAG_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_STABLE or
            View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN)
    }

    // ------------------------------------------------------------------ PlayerRuntime.Ui
    override fun onRuntimeChanged() {
        if (!visible) return
        when (runtime.phase) {
            PlayerRuntime.Phase.NEEDS_SETUP -> {
                controller.stop()
                if (!setupLaunched) {
                    setupLaunched = true
                    startActivity(Intent(this, SetupActivity::class.java))
                }
            }
            PlayerRuntime.Phase.PAIRING -> {
                controller.stop()
                val p = runtime.pairing
                val qr = p?.let { req ->
                    runtime.config.panelBaseUrl?.let { "$it/telas/parear?code=${req.pairingCode}" } ?: req.panelPairUrl
                }
                pairingOverlay.show(
                    getString(R.string.pairing_title),
                    p?.pairingCode,
                    qr,
                    getString(R.string.pairing_hint),
                    runtime.pairingError?.let { getString(R.string.pairing_error, it) },
                )
            }
            PlayerRuntime.Phase.PAIRED -> {
                setupLaunched = false
                pairingOverlay.visibility = View.GONE
                applyLayout(content.width, content.height)
                if (!controller.isRunning) controller.start()
            }
        }
        refreshAlerts()
    }

    override fun onKill(creativeIds: Set<String>) = controller.onKill(creativeIds)

    override fun onReloadRequested(hard: Boolean) {
        applyLayout(content.width, content.height)
        if (hard) controller.reload() else controller.refresh()
    }

    private fun refreshAlerts() {
        val gen = runtime.active
        val now = runtime.clock.now()
        footer.tickClock(now, gen?.manifest?.timezone)
        val days = OfflineAlerts.daysWithoutSync(now, runtime.state.lastSyncMs)
        val level = OfflineAlerts.level(now, runtime.state.lastSyncMs, gen?.manifest?.settings?.offlineAlertDays ?: listOf(7, 28))
        val clockBad = runtime.clock.clockAlert(gen?.manifest?.generatedAtMs)
        footer.setAlert(
            when {
                clockBad -> getString(R.string.alert_clock)
                level == OfflineAlerts.Level.FOOTER -> resources.getQuantityString(R.plurals.alert_offline_footer, days.toInt(), days.toInt())
                else -> null
            },
        )
        centerAlert.show(
            when {
                level == OfflineAlerts.Level.CENTER -> resources.getQuantityString(R.plurals.alert_offline_center, days.toInt(), days.toInt())
                clockBad && gen == null -> getString(R.string.alert_clock)
                else -> null
            },
        )
    }

    // ------------------------------------------------------------------ controle remoto
    override fun onKeyDown(keyCode: Int, event: KeyEvent): Boolean {
        when (keyCode) {
            KeyEvent.KEYCODE_SETTINGS -> {
                startActivity(Intent(this, WifiActivity::class.java)) // Wi-Fi sem PIN
                return true
            }
            KeyEvent.KEYCODE_MENU, KeyEvent.KEYCODE_DPAD_CENTER, KeyEvent.KEYCODE_ENTER,
            KeyEvent.KEYCODE_BUTTON_A, KeyEvent.KEYCODE_INFO -> {
                showOptions()
                return true
            }
            KeyEvent.KEYCODE_BACK -> {
                if (app.kiosk.isKioskActive) return true
                showOptions()
                return true
            }
        }
        return super.onKeyDown(keyCode, event)
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        if (event.action == MotionEvent.ACTION_UP) showOptions()
        return true
    }

    private fun showOptions() {
        val items = mutableListOf(getString(R.string.menu_wifi), getString(R.string.menu_tech))
        if (runtime.phase == PlayerRuntime.Phase.PAIRING) items += getString(R.string.menu_pair_login)
        if (!app.kiosk.isKioskActive) items += getString(R.string.menu_exit)
        AlertDialog.Builder(this)
            .setTitle(R.string.menu_title)
            .setItems(items.toTypedArray()) { _, which ->
                when (items[which]) {
                    getString(R.string.menu_wifi) -> startActivity(Intent(this, WifiActivity::class.java))
                    getString(R.string.menu_tech) -> askPinThen { startActivity(Intent(this, TechMenuActivity::class.java)) }
                    getString(R.string.menu_pair_login) -> startActivity(Intent(this, PairingLoginActivity::class.java))
                    getString(R.string.menu_exit) -> moveTaskToBack(true)
                }
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    /** PIN técnico (hash PBKDF2); 5 erros ⇒ bloqueio de 5 minutos. Sem PIN e sem kiosk ⇒ acesso livre (APK comum). */
    private fun askPinThen(action: () -> Unit) {
        val cfg = runtime.config
        val hash = cfg.techPinHash
        if (hash == null) {
            if (!app.kiosk.isKioskActive) { action(); return }
            Toast.makeText(this, R.string.pin_not_configured, Toast.LENGTH_LONG).show()
            startActivity(Intent(this, SetupActivity::class.java).putExtra(SetupActivity.EXTRA_PIN_ONLY, true))
            return
        }
        if (System.currentTimeMillis() < cfg.pinLockedUntilWallMs) {
            Toast.makeText(this, R.string.pin_locked, Toast.LENGTH_LONG).show()
            return
        }
        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_VARIATION_PASSWORD
            hint = getString(R.string.pin_hint)
        }
        AlertDialog.Builder(this)
            .setTitle(R.string.pin_title)
            .setView(input)
            .setPositiveButton(R.string.ok) { _, _ ->
                val pin = input.text.toString()
                // PBKDF2 com 210 000 iterações: fora da thread principal (evita ANR em TV Box)
                Thread {
                    val ok = PinHasher.verify(pin, hash)
                    runOnUiThread {
                        if (ok) {
                            cfg.pinFailures = 0
                            action()
                        } else {
                            val f = cfg.pinFailures + 1
                            cfg.pinFailures = f
                            if (f >= 5) {
                                cfg.pinLockedUntilWallMs = System.currentTimeMillis() + 5 * 60_000L
                                cfg.pinFailures = 0
                            }
                            Toast.makeText(this, R.string.pin_wrong, Toast.LENGTH_SHORT).show()
                        }
                    }
                }.start()
            }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }
}
