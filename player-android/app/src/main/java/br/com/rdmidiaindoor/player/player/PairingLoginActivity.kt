package br.com.rdmidiaindoor.player.player

import android.app.Activity
import android.os.Bundle
import android.text.InputType
import android.widget.Toast
import br.com.rdmidiaindoor.player.PlayerApp
import br.com.rdmidiaindoor.player.R
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Pareamento pelo técnico NO DISPOSITIVO (RDPP §2.3b): login controlado de usuário com `screens.pair`.
 * As credenciais são enviadas uma única vez e NUNCA armazenadas.
 */
class PairingLoginActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val runtime = (application as PlayerApp).runtime
        val f = FormBuilder(this)
        f.title(getString(R.string.pair_login_title))
        f.text(getString(R.string.pair_login_hint))
        val screen = f.input(getString(R.string.field_screen_code), null, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_CHARACTERS)
        val login = f.input(getString(R.string.field_login), null, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_EMAIL_ADDRESS)
        val password = f.input(getString(R.string.field_password), null, InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD)
        val totp = f.input(getString(R.string.field_totp), null, InputType.TYPE_CLASS_NUMBER)
        f.button(getString(R.string.pair_login_submit)) {
            val s = screen.text.toString().trim()
            val l = login.text.toString().trim()
            val p = password.text.toString()
            val t = totp.text.toString().trim()
            password.setText("")
            runtime.scope.launch {
                val msg = try {
                    runtime.claimWithLogin(l, p, t.ifBlank { null }, s)
                    getString(R.string.pair_login_ok)
                } catch (e: Exception) {
                    getString(R.string.pair_login_fail, e.message ?: e.javaClass.simpleName)
                }
                withContext(Dispatchers.Main) {
                    Toast.makeText(this@PairingLoginActivity, msg, Toast.LENGTH_LONG).show()
                    if (msg == getString(R.string.pair_login_ok)) finish()
                }
            }
        }
        f.button(getString(R.string.back)) { finish() }
        f.show()
    }
}
