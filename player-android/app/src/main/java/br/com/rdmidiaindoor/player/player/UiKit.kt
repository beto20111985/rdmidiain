package br.com.rdmidiaindoor.player.player

import android.app.Activity
import android.graphics.Color
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

/** Construção de telas simples, navegáveis por controle remoto (D-pad), sem dependências de UI extras. */
class FormBuilder(private val activity: Activity) {
    val column = LinearLayout(activity).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(64, 48, 64, 48)
        setBackgroundColor(Color.rgb(12, 18, 32))
    }
    val root = ScrollView(activity).apply {
        setBackgroundColor(Color.rgb(12, 18, 32))
        isFillViewport = true
        addView(column)
    }

    fun title(text: String): TextView = TextView(activity).apply {
        this.text = text
        textSize = 28f
        setTextColor(Color.WHITE)
        setPadding(0, 0, 0, 24)
        column.addView(this)
    }

    fun text(text: String, size: Float = 18f, color: Int = Color.rgb(200, 210, 230)): TextView = TextView(activity).apply {
        this.text = text
        textSize = size
        setTextColor(color)
        setPadding(0, 8, 0, 8)
        column.addView(this)
    }

    fun input(hint: String, value: String? = null, type: Int = android.text.InputType.TYPE_CLASS_TEXT): EditText = EditText(activity).apply {
        this.hint = hint
        setText(value ?: "")
        inputType = type
        setTextColor(Color.WHITE)
        setHintTextColor(Color.rgb(120, 130, 150))
        isSingleLine = true
        column.addView(this, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT))
    }

    fun button(text: String, onClick: () -> Unit): Button = Button(activity).apply {
        this.text = text
        isAllCaps = false
        gravity = Gravity.CENTER_VERTICAL or Gravity.START
        setOnClickListener { onClick() }
        column.addView(this, LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT).apply { topMargin = 8 })
    }

    fun show() {
        activity.setContentView(root)
        // primeiro elemento focável recebe o foco (controle remoto)
        for (i in 0 until column.childCount) {
            val v: View = column.getChildAt(i)
            if (v.isFocusable) { v.requestFocus(); break }
        }
    }
}
