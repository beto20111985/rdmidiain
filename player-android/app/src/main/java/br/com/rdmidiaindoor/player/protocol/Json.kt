package br.com.rdmidiaindoor.player.protocol

import java.math.BigInteger

/** Erro de protocolo (JSON inválido, campo ausente, tipo errado, float proibido…). [code] é estável. */
class ProtocolException(val code: String, message: String) : Exception("$code: $message")

/**
 * Árvore JSON mínima e estrita usada pelo RDPP/1. Números guardam o lexema e, quando inteiros,
 * o valor exato ([BigInteger]) — necessário para a forma canônica RDJ1 (somente inteiros).
 * Puro Kotlin/JVM (sem android.*) para ser testável em JVM.
 */
sealed class JsonValue {
    object Null : JsonValue() {
        override fun toString() = "null"
    }

    data class Bool(val value: Boolean) : JsonValue()

    /** [integer] == null ⇒ número não inteiro (float) — proibido em RDJ1. */
    data class Num(val lexeme: String, val integer: BigInteger?) : JsonValue() {
        val isInteger: Boolean get() = integer != null

        fun toLong(): Long {
            val i = integer ?: throw ProtocolException("FLOAT_NOT_ALLOWED", "número não inteiro: $lexeme")
            if (i.bitLength() > 63) throw ProtocolException("INT_OUT_OF_RANGE", lexeme)
            return i.toLong()
        }

        companion object {
            fun of(v: Long) = Num(v.toString(), BigInteger.valueOf(v))
        }
    }

    data class Str(val value: String) : JsonValue()

    data class Arr(val items: List<JsonValue>) : JsonValue()

    /** Ordem de inserção preservada (LinkedHashMap); a ordem canônica é aplicada só na serialização RDJ1. */
    data class Obj(val fields: Map<String, JsonValue>) : JsonValue() {
        operator fun get(key: String): JsonValue? = fields[key]
        fun has(key: String) = fields.containsKey(key)

        private fun req(key: String): JsonValue =
            fields[key] ?: throw ProtocolException("MISSING_FIELD", "campo obrigatório ausente: $key")

        fun str(key: String): String = (req(key) as? Str)?.value
            ?: throw ProtocolException("BAD_TYPE", "$key deve ser string")

        fun strOrNull(key: String): String? = when (val v = fields[key]) {
            null, Null -> null
            is Str -> v.value
            else -> throw ProtocolException("BAD_TYPE", "$key deve ser string ou null")
        }

        fun long(key: String): Long = (req(key) as? Num)?.toLong()
            ?: throw ProtocolException("BAD_TYPE", "$key deve ser inteiro")

        fun longOrNull(key: String): Long? = when (val v = fields[key]) {
            null, Null -> null
            is Num -> v.toLong()
            else -> throw ProtocolException("BAD_TYPE", "$key deve ser inteiro ou null")
        }

        fun int(key: String): Int = long(key).toInt()

        fun bool(key: String): Boolean = (req(key) as? Bool)?.value
            ?: throw ProtocolException("BAD_TYPE", "$key deve ser booleano")

        fun boolOrNull(key: String): Boolean? = when (val v = fields[key]) {
            null, Null -> null
            is Bool -> v.value
            else -> throw ProtocolException("BAD_TYPE", "$key deve ser booleano ou null")
        }

        fun obj(key: String): Obj = req(key) as? Obj
            ?: throw ProtocolException("BAD_TYPE", "$key deve ser objeto")

        fun objOrNull(key: String): Obj? = when (val v = fields[key]) {
            null, Null -> null
            is Obj -> v
            else -> throw ProtocolException("BAD_TYPE", "$key deve ser objeto ou null")
        }

        fun arr(key: String): List<JsonValue> = (req(key) as? Arr)?.items
            ?: throw ProtocolException("BAD_TYPE", "$key deve ser lista")

        fun arrOrEmpty(key: String): List<JsonValue> = when (val v = fields[key]) {
            null, Null -> emptyList()
            is Arr -> v.items
            else -> throw ProtocolException("BAD_TYPE", "$key deve ser lista")
        }
    }
}

/** Construção ergonômica de objetos JSON a partir de valores Kotlin. */
fun jsonOf(vararg pairs: Pair<String, Any?>): JsonValue.Obj {
    val map = LinkedHashMap<String, JsonValue>()
    for ((k, v) in pairs) map[k] = Json.from(v)
    return JsonValue.Obj(map)
}

object Json {
    private const val MAX_DEPTH = 64

    fun from(v: Any?): JsonValue = when (v) {
        null -> JsonValue.Null
        is JsonValue -> v
        is String -> JsonValue.Str(v)
        is Boolean -> JsonValue.Bool(v)
        is Int -> JsonValue.Num.of(v.toLong())
        is Long -> JsonValue.Num.of(v)
        is BigInteger -> JsonValue.Num(v.toString(), v)
        is Map<*, *> -> {
            val m = LinkedHashMap<String, JsonValue>()
            for ((k, value) in v) m[k as String] = from(value)
            JsonValue.Obj(m)
        }
        is Iterable<*> -> JsonValue.Arr(v.map { from(it) })
        is Array<*> -> JsonValue.Arr(v.map { from(it) })
        else -> throw IllegalArgumentException("tipo não suportado em JSON: ${v::class.java}")
    }

    fun parse(text: String): JsonValue {
        val p = Parser(text)
        p.skipWs()
        val v = p.value(0)
        p.skipWs()
        if (!p.atEnd()) throw ProtocolException("JSON_INVALID", "conteúdo após o fim na posição ${p.pos}")
        return v
    }

    fun parse(bytes: ByteArray): JsonValue = parse(String(bytes, Charsets.UTF_8))

    fun parseObject(text: String): JsonValue.Obj =
        parse(text) as? JsonValue.Obj ?: throw ProtocolException("JSON_INVALID", "esperado objeto JSON")

    fun parseObject(bytes: ByteArray): JsonValue.Obj = parseObject(String(bytes, Charsets.UTF_8))

    /**
     * Serialização compacta NÃO canônica (ordem de inserção; aceita floats pelo lexema).
     * Para assinatura/hash use sempre [Rdj1].
     */
    fun stringify(v: JsonValue): String = StringBuilder().also { write(it, v) }.toString()

    private fun write(sb: StringBuilder, v: JsonValue) {
        when (v) {
            JsonValue.Null -> sb.append("null")
            is JsonValue.Bool -> sb.append(if (v.value) "true" else "false")
            is JsonValue.Num -> sb.append(v.integer?.toString() ?: v.lexeme)
            is JsonValue.Str -> Rdj1.appendString(sb, v.value)
            is JsonValue.Arr -> {
                sb.append('[')
                v.items.forEachIndexed { i, item -> if (i > 0) sb.append(','); write(sb, item) }
                sb.append(']')
            }
            is JsonValue.Obj -> {
                sb.append('{')
                var first = true
                for ((k, item) in v.fields) {
                    if (!first) sb.append(',')
                    first = false
                    Rdj1.appendString(sb, k)
                    sb.append(':')
                    write(sb, item)
                }
                sb.append('}')
            }
        }
    }

    private class Parser(val s: String) {
        var pos = 0

        fun atEnd() = pos >= s.length

        fun skipWs() {
            while (pos < s.length) {
                val c = s[pos]
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++ else break
            }
        }

        private fun fail(msg: String): Nothing = throw ProtocolException("JSON_INVALID", "$msg na posição $pos")

        fun value(depth: Int): JsonValue {
            if (depth > MAX_DEPTH) fail("profundidade excessiva")
            if (atEnd()) fail("fim inesperado")
            return when (val c = s[pos]) {
                '{' -> obj(depth)
                '[' -> arr(depth)
                '"' -> JsonValue.Str(string())
                't' -> literal("true", JsonValue.Bool(true))
                'f' -> literal("false", JsonValue.Bool(false))
                'n' -> literal("null", JsonValue.Null)
                else -> if (c == '-' || c in '0'..'9') number() else fail("caractere inesperado '$c'")
            }
        }

        private fun literal(word: String, v: JsonValue): JsonValue {
            if (!s.startsWith(word, pos)) fail("literal inválido")
            pos += word.length
            return v
        }

        private fun obj(depth: Int): JsonValue.Obj {
            pos++ // {
            val map = LinkedHashMap<String, JsonValue>()
            skipWs()
            if (!atEnd() && s[pos] == '}') { pos++; return JsonValue.Obj(map) }
            while (true) {
                skipWs()
                if (atEnd() || s[pos] != '"') fail("esperada chave string")
                val key = string()
                skipWs()
                if (atEnd() || s[pos] != ':') fail("esperado ':'")
                pos++
                skipWs()
                val v = value(depth + 1)
                if (map.containsKey(key)) throw ProtocolException("JSON_DUPLICATE_KEY", "chave duplicada: $key")
                map[key] = v
                skipWs()
                if (atEnd()) fail("objeto não terminado")
                when (s[pos]) {
                    ',' -> pos++
                    '}' -> { pos++; return JsonValue.Obj(map) }
                    else -> fail("esperado ',' ou '}'")
                }
            }
        }

        private fun arr(depth: Int): JsonValue.Arr {
            pos++ // [
            val list = ArrayList<JsonValue>()
            skipWs()
            if (!atEnd() && s[pos] == ']') { pos++; return JsonValue.Arr(list) }
            while (true) {
                skipWs()
                list.add(value(depth + 1))
                skipWs()
                if (atEnd()) fail("lista não terminada")
                when (s[pos]) {
                    ',' -> pos++
                    ']' -> { pos++; return JsonValue.Arr(list) }
                    else -> fail("esperado ',' ou ']'")
                }
            }
        }

        private fun string(): String {
            pos++ // "
            val sb = StringBuilder()
            while (true) {
                if (atEnd()) fail("string não terminada")
                val c = s[pos++]
                when {
                    c == '"' -> return sb.toString()
                    c == '\\' -> {
                        if (atEnd()) fail("escape incompleto")
                        when (val e = s[pos++]) {
                            '"' -> sb.append('"')
                            '\\' -> sb.append('\\')
                            '/' -> sb.append('/')
                            'b' -> sb.append('\b')
                            'f' -> sb.append('\u000C')
                            'n' -> sb.append('\n')
                            'r' -> sb.append('\r')
                            't' -> sb.append('\t')
                            'u' -> {
                                if (pos + 4 > s.length) fail("escape \\u incompleto")
                                val hex = s.substring(pos, pos + 4)
                                val code = hex.toIntOrNull(16) ?: fail("escape \\u inválido")
                                sb.append(code.toChar())
                                pos += 4
                            }
                            else -> fail("escape inválido '\\$e'")
                        }
                    }
                    c < ' ' -> fail("caractere de controle não escapado")
                    else -> sb.append(c)
                }
            }
        }

        private fun number(): JsonValue.Num {
            val start = pos
            if (s[pos] == '-') pos++
            if (atEnd()) fail("número incompleto")
            if (s[pos] == '0') {
                pos++
            } else if (s[pos] in '1'..'9') {
                while (!atEnd() && s[pos] in '0'..'9') pos++
            } else fail("número inválido")
            var isInt = true
            if (!atEnd() && s[pos] == '.') {
                isInt = false
                pos++
                val d0 = pos
                while (!atEnd() && s[pos] in '0'..'9') pos++
                if (pos == d0) fail("fração inválida")
            }
            if (!atEnd() && (s[pos] == 'e' || s[pos] == 'E')) {
                isInt = false
                pos++
                if (!atEnd() && (s[pos] == '+' || s[pos] == '-')) pos++
                val d0 = pos
                while (!atEnd() && s[pos] in '0'..'9') pos++
                if (pos == d0) fail("expoente inválido")
            }
            val lex = s.substring(start, pos)
            return JsonValue.Num(lex, if (isInt) BigInteger(lex) else null)
        }
    }
}
