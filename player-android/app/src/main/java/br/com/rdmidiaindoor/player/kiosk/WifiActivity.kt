package br.com.rdmidiaindoor.player.kiosk

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.wifi.ScanResult
import android.net.wifi.WifiConfiguration
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.InputType
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.core.content.ContextCompat
import br.com.rdmidiaindoor.player.R
import br.com.rdmidiaindoor.player.player.FormBuilder

/**
 * Tela própria de Wi-Fi, acessível SEM PIN (cliente só pode trocar a rede — ETAPA2 #240–#245).
 *
 * Limitações por versão (documentadas no README):
 * - API ≤ 28: `WifiManager.addNetwork/enableNetwork` funcionam para qualquer app.
 * - API ≥ 29: essas APIs só funcionam para Device Owner/Profile Owner/apps de sistema. No modo kiosk (DO) o app
 *   conecta diretamente; no modo APK comum abre o painel do sistema (`Settings.Panel`/configurações de Wi-Fi).
 * - Listar redes exige permissão de localização (DO concede automaticamente) e, em vários aparelhos, localização
 *   ligada; `startScan` é limitado pelo sistema (throttling) a partir da API 28.
 */
class WifiActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var list: LinearLayout
    private lateinit var wifi: WifiManager
    private var receiverRegistered = false
    private val scanReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) = renderResults()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val kiosk = KioskManager(this)
        val f = FormBuilder(this)
        f.title(getString(R.string.wifi_title))
        status = f.text("")
        f.button(getString(R.string.wifi_scan)) { scan() }
        list = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        f.column.addView(list)
        if (!kiosk.isKioskActive) {
            f.button(getString(R.string.wifi_system_settings)) { openSystemPanel() }
        }
        f.button(getString(R.string.back)) { finish() }
        f.show()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.ACCESS_FINE_LOCATION) != PackageManager.PERMISSION_GRANTED &&
            !kiosk.isDeviceOwner
        ) {
            requestPermissions(arrayOf(Manifest.permission.ACCESS_FINE_LOCATION), 1)
        }
    }

    override fun onResume() {
        super.onResume()
        ContextCompat.registerReceiver(this, scanReceiver, IntentFilter(WifiManager.SCAN_RESULTS_AVAILABLE_ACTION), ContextCompat.RECEIVER_EXPORTED)
        receiverRegistered = true
        renderStatus()
        renderResults()
    }

    override fun onPause() {
        if (receiverRegistered) unregisterReceiver(scanReceiver)
        receiverRegistered = false
        super.onPause()
    }

    @Suppress("DEPRECATION")
    private fun renderStatus() {
        val cm = getSystemService(CONNECTIVITY_SERVICE) as ConnectivityManager
        val caps = cm.getNetworkCapabilities(cm.activeNetwork)
        val text = when {
            caps == null -> getString(R.string.net_offline)
            caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET) -> getString(R.string.wifi_ethernet)
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) -> {
                val ssid = wifi.connectionInfo?.ssid?.removeSurrounding("\"")?.takeIf { it != "<unknown ssid>" } ?: "?"
                getString(R.string.wifi_connected, ssid, wifi.connectionInfo?.rssi ?: 0)
            }
            else -> getString(R.string.net_online)
        }
        status.text = if (!wifi.isWifiEnabled) "$text — ${getString(R.string.wifi_disabled)}" else text
    }

    @Suppress("DEPRECATION")
    private fun scan() {
        if (!wifi.isWifiEnabled) {
            // permitido a qualquer app até a API 28; na API 29+ apenas DO/sistema
            if (!wifi.setWifiEnabled(true)) {
                Toast.makeText(this, R.string.wifi_enable_failed, Toast.LENGTH_LONG).show()
                return
            }
        }
        if (!wifi.startScan()) Toast.makeText(this, R.string.wifi_scan_throttled, Toast.LENGTH_SHORT).show()
        renderResults()
    }

    @SuppressLint("MissingPermission")
    private fun renderResults() {
        list.removeAllViews()
        val results: List<ScanResult> = try { wifi.scanResults.orEmpty() } catch (e: SecurityException) { emptyList() }
        val best = results.filter { ssidOf(it).isNotBlank() }.groupBy { ssidOf(it) }.map { (_, v) -> v.maxBy { it.level } }
            .sortedByDescending { it.level }
        if (best.isEmpty()) {
            list.addView(TextView(this).apply { text = getString(R.string.wifi_no_results) })
            return
        }
        for (r in best) {
            val secured = r.capabilities.contains("WEP") || r.capabilities.contains("PSK") || r.capabilities.contains("SAE") || r.capabilities.contains("EAP")
            val b = android.widget.Button(this).apply {
                isAllCaps = false
                text = getString(if (secured) R.string.wifi_item_secured else R.string.wifi_item_open, ssidOf(r), r.level)
                setOnClickListener { connectDialog(ssidOf(r), r.capabilities) }
            }
            list.addView(b)
        }
    }

    @Suppress("DEPRECATION")
    private fun ssidOf(r: ScanResult): String = r.SSID ?: ""

    private fun connectDialog(ssid: String, capabilities: String) {
        val open = !(capabilities.contains("WEP") || capabilities.contains("PSK") || capabilities.contains("SAE") || capabilities.contains("EAP"))
        if (open) {
            connect(ssid, null, capabilities)
            return
        }
        val input = EditText(this).apply { inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD }
        AlertDialog.Builder(this)
            .setTitle(getString(R.string.wifi_password_for, ssid))
            .setView(input)
            .setPositiveButton(R.string.ok) { _, _ -> connect(ssid, input.text.toString(), capabilities) }
            .setNegativeButton(R.string.cancel, null)
            .show()
    }

    /** DO (qualquer API) ou API ≤ 28: conecta direto. Senão: painel do sistema (modo APK comum). */
    @Suppress("DEPRECATION")
    @SuppressLint("MissingPermission")
    private fun connect(ssid: String, password: String?, capabilities: String) {
        val canDirect = Build.VERSION.SDK_INT < Build.VERSION_CODES.Q || KioskManager(this).isDeviceOwner
        if (!canDirect) {
            openSystemPanel()
            return
        }
        val conf = WifiConfiguration().apply {
            SSID = "\"$ssid\""
            when {
                password == null -> allowedKeyManagement.set(WifiConfiguration.KeyMgmt.NONE)
                capabilities.contains("WEP") -> {
                    wepKeys[0] = "\"$password\""
                    wepTxKeyIndex = 0
                    allowedKeyManagement.set(WifiConfiguration.KeyMgmt.NONE)
                    allowedAuthAlgorithms.set(WifiConfiguration.AuthAlgorithm.SHARED)
                }
                else -> preSharedKey = "\"$password\""
            }
        }
        val existing = try { wifi.configuredNetworks?.firstOrNull { it.SSID == conf.SSID } } catch (e: SecurityException) { null }
        val id = if (existing != null) {
            conf.networkId = existing.networkId
            wifi.updateNetwork(conf)
        } else wifi.addNetwork(conf)
        val ok = id != -1 && wifi.enableNetwork(id, true) && wifi.reconnect()
        Toast.makeText(this, if (ok) getString(R.string.wifi_connecting, ssid) else getString(R.string.wifi_connect_failed), Toast.LENGTH_LONG).show()
    }

    private fun openSystemPanel() {
        val intent = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) Intent(Settings.Panel.ACTION_WIFI) else Intent(Settings.ACTION_WIFI_SETTINGS)
        try {
            startActivity(intent)
        } catch (e: Exception) {
            try { startActivity(Intent(Settings.ACTION_WIFI_SETTINGS)) } catch (_: Exception) {
                Toast.makeText(this, R.string.not_available_on_device, Toast.LENGTH_LONG).show()
            }
        }
    }
}
