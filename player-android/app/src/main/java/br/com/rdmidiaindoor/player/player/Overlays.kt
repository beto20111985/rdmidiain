package br.com.rdmidiaindoor.player.player

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Color
import android.graphics.Typeface
import android.util.TypedValue
import android.view.Gravity
import android.view.View
import android.widget.FrameLayout
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/** QR gerado LOCALMENTE (ZXing core), sem serviço externo. */
object QrCode {
    fun bitmap(content: String, sizePx: Int): Bitmap {
        val hints = mapOf(EncodeHintType.ERROR_CORRECTION to ErrorCorrectionLevel.M, EncodeHintType.MARGIN to 2, EncodeHintType.CHARACTER_SET to "UTF-8")
        val m = QRCodeWriter().encode(content, BarcodeFormat.QR_CODE, sizePx, sizePx, hints)
        val pixels = IntArray(m.width * m.height)
        for (y in 0 until m.height) for (x in 0 until m.width) pixels[y * m.width + x] = if (m.get(x, y)) Color.BLACK else Color.WHITE
        return Bitmap.createBitmap(pixels, m.width, m.height, Bitmap.Config.RGB_565)
    }
}

/**
 * Rodapé (RDPP §5.1 item 10; ETAPA2 #359–#365): tarja preta, texto branco `"<label> — <code>"`, relógio à direita
 * e espaço para alertas (aviso de 7 dias destacado). A área útil do anúncio é reduzida pela altura do rodapé.
 */
class FooterView(context: Context) : LinearLayout(context) {
    private val label = TextView(context)
    private val alert = TextView(context)
    private val clock = TextView(context)
    private val clockFmt = SimpleDateFormat("HH:mm", Locale.getDefault())

    init {
        orientation = HORIZONTAL
        gravity = Gravity.CENTER_VERTICAL
        setBackgroundColor(Color.BLACK)
        listOf(label, alert, clock).forEach {
            it.setTextColor(Color.WHITE)
            it.maxLines = 1
            it.includeFontPadding = false
        }
        alert.setTypeface(alert.typeface, Typeface.BOLD)
        alert.setTextColor(Color.BLACK)
        alert.setBackgroundColor(Color.rgb(255, 196, 0))
        alert.visibility = GONE
        addView(label, LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f))
        addView(alert, LayoutParams(LayoutParams.WRAP_CONTENT, LayoutParams.WRAP_CONTENT))
        addView(clock, LayoutParams(LayoutParams.WRAP_CONTENT, LayoutParams.WRAP_CONTENT))
    }

    fun applyHeight(heightPx: Int) {
        val ts = heightPx * 0.45f
        listOf(label, alert, clock).forEach { it.setTextSize(TypedValue.COMPLEX_UNIT_PX, ts) }
        val pad = (heightPx * 0.3f).toInt()
        setPadding(pad, 0, pad, 0)
        alert.setPadding(pad / 2, 0, pad / 2, 0)
        (alert.layoutParams as LayoutParams).marginEnd = pad
    }

    fun setLabel(text: String) {
        label.text = text
    }

    fun setAlert(text: String?) {
        alert.text = text ?: ""
        alert.visibility = if (text.isNullOrEmpty()) GONE else VISIBLE
    }

    fun tickClock(nowMs: Long, timezone: String?) {
        clockFmt.timeZone = timezone?.let { TimeZone.getTimeZone(it) } ?: TimeZone.getDefault()
        clock.text = clockFmt.format(Date(nowMs))
    }
}

/** Overlay de pareamento: código grande + QR do `panel_pair_url` + instruções. */
class PairingOverlay(context: Context) : LinearLayout(context) {
    private val title = TextView(context)
    private val code = TextView(context)
    private val qr = ImageView(context)
    private val hint = TextView(context)
    private val status = TextView(context)

    init {
        orientation = VERTICAL
        gravity = Gravity.CENTER
        setBackgroundColor(Color.rgb(12, 18, 32))
        title.setTextColor(Color.WHITE); title.textSize = 28f; title.gravity = Gravity.CENTER
        code.setTextColor(Color.WHITE); code.textSize = 64f; code.setTypeface(Typeface.MONOSPACE, Typeface.BOLD); code.gravity = Gravity.CENTER
        hint.setTextColor(Color.rgb(170, 190, 220)); hint.textSize = 18f; hint.gravity = Gravity.CENTER
        status.setTextColor(Color.rgb(255, 196, 0)); status.textSize = 16f; status.gravity = Gravity.CENTER
        addView(title)
        addView(code)
        addView(qr, LayoutParams(420, 420).apply { topMargin = 24; bottomMargin = 24 })
        addView(hint)
        addView(status)
    }

    fun show(titleText: String, pairingCode: String?, qrContent: String?, hintText: String, statusText: String?) {
        title.text = titleText
        code.text = pairingCode ?: "…"
        hint.text = hintText
        status.text = statusText ?: ""
        if (qrContent != null) {
            qr.setImageBitmap(QrCode.bitmap(qrContent, 420))
            qr.visibility = View.VISIBLE
        } else qr.visibility = View.INVISIBLE
        visibility = View.VISIBLE
    }
}

/** Alerta central grande (≥ 28 dias sem sincronizar; relógio inválido). */
class CenterAlert(context: Context) : TextView(context) {
    init {
        setBackgroundColor(Color.argb(235, 160, 20, 20))
        setTextColor(Color.WHITE)
        textSize = 40f
        gravity = Gravity.CENTER
        setPadding(48, 48, 48, 48)
        visibility = View.GONE
    }

    fun show(text: String?) {
        this.text = text ?: ""
        visibility = if (text.isNullOrEmpty()) View.GONE else View.VISIBLE
    }
}

/**
 * Container que gira o conteúdo 90° quando a tela é vertical mas a saída HDMI é paisagem (rotação por software
 * da superfície de render — A4 §9.3). Mede o filho com largura/altura trocadas.
 */
class RotatableContainer(context: Context) : FrameLayout(context) {
    var portrait: Boolean = false
        set(v) {
            field = v
            requestLayout()
        }

    override fun onMeasure(widthMeasureSpec: Int, heightMeasureSpec: Int) {
        val w = MeasureSpec.getSize(widthMeasureSpec)
        val h = MeasureSpec.getSize(heightMeasureSpec)
        setMeasuredDimension(w, h)
        val rotate = portrait && w > h
        val cw = if (rotate) h else w
        val ch = if (rotate) w else h
        for (i in 0 until childCount) {
            getChildAt(i).measure(MeasureSpec.makeMeasureSpec(cw, MeasureSpec.EXACTLY), MeasureSpec.makeMeasureSpec(ch, MeasureSpec.EXACTLY))
        }
    }

    override fun onLayout(changed: Boolean, left: Int, top: Int, right: Int, bottom: Int) {
        val w = right - left
        val h = bottom - top
        for (i in 0 until childCount) {
            val c = getChildAt(i)
            val cw = c.measuredWidth
            val ch = c.measuredHeight
            val l = (w - cw) / 2
            val t = (h - ch) / 2
            c.layout(l, t, l + cw, t + ch)
            val rotate = portrait && w > h
            c.pivotX = cw / 2f
            c.pivotY = ch / 2f
            c.rotation = if (rotate) 90f else 0f
        }
    }
}
