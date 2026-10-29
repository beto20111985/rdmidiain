package br.com.rdmidiaindoor.player.protocol

import java.security.MessageDigest

/**
 * JSON canônico RDJ1 (RDPP/1 §0): chaves ordenadas por PONTO DE CÓDIGO, sem espaços, escapes mínimos
 * (`\"`, `\\`, `\b`, `\f`, `\n`, `\r`, `\t`, demais controles `\u00xx` minúsculo; não-ASCII sem escape),
 * somente inteiros, booleanos e null. Bytes assinados = UTF-8 do texto.
 *
 * Observação: a comparação por ponto de código difere da ordem de `String.compareTo` (unidades UTF-16)
 * para chaves com caracteres fora do BMP vs. U+E000–U+FFFF; aqui implementamos a ordem normativa.
 */
object Rdj1 {

    fun encode(v: JsonValue): String = StringBuilder().also { write(it, v) }.toString()

    fun bytes(v: JsonValue): ByteArray = encode(v).toByteArray(Charsets.UTF_8)

    fun sha256Hex(v: JsonValue): String = Hex.encode(sha256(bytes(v)))

    fun sha256(data: ByteArray): ByteArray = MessageDigest.getInstance("SHA-256").digest(data)

    private fun write(sb: StringBuilder, v: JsonValue) {
        when (v) {
            JsonValue.Null -> sb.append("null")
            is JsonValue.Bool -> sb.append(if (v.value) "true" else "false")
            is JsonValue.Num -> {
                val i = v.integer ?: throw ProtocolException("FLOAT_NOT_ALLOWED", "float proibido em RDJ1: ${v.lexeme}")
                sb.append(i.toString())
            }
            is JsonValue.Str -> appendString(sb, v.value)
            is JsonValue.Arr -> {
                sb.append('[')
                v.items.forEachIndexed { idx, item ->
                    if (idx > 0) sb.append(',')
                    write(sb, item)
                }
                sb.append(']')
            }
            is JsonValue.Obj -> {
                sb.append('{')
                val keys = v.fields.keys.sortedWith(CODE_POINT_ORDER)
                keys.forEachIndexed { idx, k ->
                    if (idx > 0) sb.append(',')
                    appendString(sb, k)
                    sb.append(':')
                    write(sb, v.fields.getValue(k))
                }
                sb.append('}')
            }
        }
    }

    val CODE_POINT_ORDER: Comparator<String> = Comparator { a, b -> compareCodePoints(a, b) }

    fun compareCodePoints(a: String, b: String): Int {
        var i = 0
        var j = 0
        while (i < a.length && j < b.length) {
            val ca = a.codePointAt(i)
            val cb = b.codePointAt(j)
            if (ca != cb) return ca.compareTo(cb)
            i += Character.charCount(ca)
            j += Character.charCount(cb)
        }
        val restA = i < a.length
        val restB = j < b.length
        return when {
            restA == restB -> 0
            restA -> 1
            else -> -1
        }
    }

    private const val HEX = "0123456789abcdef"

    fun appendString(sb: StringBuilder, s: String) {
        sb.append('"')
        var i = 0
        while (i < s.length) {
            val c = s[i]
            when {
                c == '"' -> sb.append("\\\"")
                c == '\\' -> sb.append("\\\\")
                c == '\b' -> sb.append("\\b")
                c == '\u000C' -> sb.append("\\f")
                c == '\n' -> sb.append("\\n")
                c == '\r' -> sb.append("\\r")
                c == '\t' -> sb.append("\\t")
                c < ' ' -> {
                    sb.append("\\u00")
                    sb.append(HEX[(c.code shr 4) and 0xF])
                    sb.append(HEX[c.code and 0xF])
                }
                Character.isHighSurrogate(c) -> {
                    if (i + 1 >= s.length || !Character.isLowSurrogate(s[i + 1])) {
                        throw ProtocolException("INVALID_UNICODE", "surrogate alto sem par")
                    }
                    sb.append(c).append(s[i + 1])
                    i++
                }
                Character.isLowSurrogate(c) -> throw ProtocolException("INVALID_UNICODE", "surrogate baixo isolado")
                else -> sb.append(c)
            }
            i++
        }
        sb.append('"')
    }
}

object Hex {
    private const val DIGITS = "0123456789abcdef"

    fun encode(data: ByteArray): String {
        val sb = StringBuilder(data.size * 2)
        for (b in data) {
            val v = b.toInt() and 0xFF
            sb.append(DIGITS[v ushr 4]).append(DIGITS[v and 0xF])
        }
        return sb.toString()
    }

    fun decode(s: String): ByteArray {
        require(s.length % 2 == 0) { "hex de tamanho ímpar" }
        return ByteArray(s.length / 2) { i ->
            val hi = Character.digit(s[2 * i], 16)
            val lo = Character.digit(s[2 * i + 1], 16)
            require(hi >= 0 && lo >= 0) { "hex inválido" }
            ((hi shl 4) or lo).toByte()
        }
    }

    fun isSha256Hex(s: String): Boolean = s.length == 64 && s.all { it in '0'..'9' || it in 'a'..'f' }
}

/** Base64url sem padding (RFC 4648 §5). Implementação própria: java.util.Base64 só existe na API 26+. */
object Base64Url {
    private const val ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    private val DECODE = IntArray(128) { -1 }.also { t -> ALPHABET.forEachIndexed { i, c -> t[c.code] = i } }

    fun encode(data: ByteArray): String {
        val sb = StringBuilder((data.size * 4 + 2) / 3)
        var i = 0
        while (i + 3 <= data.size) {
            val n = ((data[i].toInt() and 0xFF) shl 16) or ((data[i + 1].toInt() and 0xFF) shl 8) or (data[i + 2].toInt() and 0xFF)
            sb.append(ALPHABET[(n shr 18) and 63]).append(ALPHABET[(n shr 12) and 63])
                .append(ALPHABET[(n shr 6) and 63]).append(ALPHABET[n and 63])
            i += 3
        }
        val rem = data.size - i
        if (rem == 1) {
            val n = (data[i].toInt() and 0xFF) shl 16
            sb.append(ALPHABET[(n shr 18) and 63]).append(ALPHABET[(n shr 12) and 63])
        } else if (rem == 2) {
            val n = ((data[i].toInt() and 0xFF) shl 16) or ((data[i + 1].toInt() and 0xFF) shl 8)
            sb.append(ALPHABET[(n shr 18) and 63]).append(ALPHABET[(n shr 12) and 63]).append(ALPHABET[(n shr 6) and 63])
        }
        return sb.toString()
    }

    /** Estrito: rejeita padding, alfabeto padrão (+ /), espaços e bits finais não nulos. */
    fun decode(s: String): ByteArray {
        if (s.length % 4 == 1) throw ProtocolException("BASE64_INVALID", "tamanho inválido")
        val out = ByteArray(s.length * 3 / 4)
        var acc = 0
        var bits = 0
        var o = 0
        for (c in s) {
            val v = if (c.code < 128) DECODE[c.code] else -1
            if (v < 0) throw ProtocolException("BASE64_INVALID", "caractere inválido '$c'")
            acc = (acc shl 6) or v
            bits += 6
            if (bits >= 8) {
                bits -= 8
                out[o++] = ((acc shr bits) and 0xFF).toByte()
            }
        }
        if (bits > 0 && (acc and ((1 shl bits) - 1)) != 0) {
            throw ProtocolException("BASE64_INVALID", "bits finais não nulos")
        }
        return if (o == out.size) out else out.copyOf(o)
    }
}
