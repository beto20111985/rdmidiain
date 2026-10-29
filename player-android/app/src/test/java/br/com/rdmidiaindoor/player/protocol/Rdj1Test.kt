package br.com.rdmidiaindoor.player.protocol

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.util.Random

class Rdj1Test {
    private fun canon(json: String) = Rdj1.encode(Json.parse(json))

    private fun expectError(code: String, block: () -> Unit) {
        try {
            block()
            fail("esperado erro $code")
        } catch (e: ProtocolException) {
            assertEquals(code, e.code)
        }
    }

    @Test
    fun sortsKeysRecursively_noWhitespace() {
        assertEquals("""{"a":[true,null,"x",{"y":2,"z":1}],"b":1}""", canon(""" { "b" : 1, "a" : [ true , null , "x", {"z":1,"y":2} ] } """))
        assertEquals("{}", canon("{ }"))
        assertEquals("[]", canon("[ ]"))
    }

    @Test
    fun minimalEscapes_lowercaseUnicode_nonAsciiRaw() {
        assertEquals("\"\\\"\\\\\\b\\f\\n\\r\\t\"", canon("\"\\\"\\\\\\b\\f\\n\\r\\t\""))
        assertEquals("\"\\u0001\\u001f\"", canon("\"\\u0001\\u001F\""))
        assertEquals("\"/\"", canon("\"\\/\""))
        assertEquals("\"Mídia — 😀\u007f\"", canon("\"M\\u00edd\\u0069a \\u2014 \\ud83d\\ude00\\u007f\""))
    }

    @Test
    fun onlyIntegers_floatsRejected_negativeZeroNormalized_bigIntegersExact() {
        expectError("FLOAT_NOT_ALLOWED") { canon("1.5") }
        expectError("FLOAT_NOT_ALLOWED") { canon("""{"a":1e3}""") }
        expectError("FLOAT_NOT_ALLOWED") { canon("""[1.0]""") }
        assertEquals("0", canon("-0"))
        assertEquals("-12", canon("-12"))
        assertEquals("123456789012345678901234567890", canon("123456789012345678901234567890"))
        assertEquals("9007199254740993", canon("9007199254740993"))
    }

    @Test
    fun keysOrderedByCodePoint_notUtf16() {
        // U+E000 (BMP) < U+1F600 por ponto de código; em UTF-16 o surrogate 0xD83D viria antes de 0xE000
        val obj = jsonOf("\uD83D\uDE00" to 1, "\uE000" to 2, "a" to 3, "B" to 4)
        assertEquals("{\"B\":4,\"a\":3,\"\uE000\":2,\"\uD83D\uDE00\":1}", Rdj1.encode(obj))
        assertTrue("\uD83D\uDE00" < "\uE000") // confirma a divergência da ordem UTF-16
    }

    @Test
    fun strictParser_rejectsInvalidJson_andDuplicates() {
        expectError("JSON_DUPLICATE_KEY") { Json.parse("""{"a":1,"a":2}""") }
        expectError("JSON_INVALID") { Json.parse("""{"a":01}""") }
        expectError("JSON_INVALID") { Json.parse("""[1,]""") }
        expectError("JSON_INVALID") { Json.parse("\"a\u0001\"") }
        expectError("JSON_INVALID") { Json.parse("""{"a":1} x""") }
        expectError("INVALID_UNICODE") { canon("\"\\ud800\"") }
    }

    @Test
    fun idempotent_roundTrip_randomTrees() {
        val rnd = Random(7)
        repeat(200) {
            val v = randomValue(rnd, 0)
            val c1 = Rdj1.encode(v)
            val c2 = Rdj1.encode(Json.parse(c1))
            assertEquals(c1, c2)
        }
    }

    private fun randomValue(r: Random, depth: Int): JsonValue = when (if (depth > 3) r.nextInt(4) else r.nextInt(6)) {
        0 -> JsonValue.Num.of(r.nextLong() shr r.nextInt(60))
        1 -> JsonValue.Str(randomString(r))
        2 -> JsonValue.Bool(r.nextBoolean())
        3 -> JsonValue.Null
        4 -> JsonValue.Arr((0 until r.nextInt(4)).map { randomValue(r, depth + 1) })
        else -> JsonValue.Obj((0 until r.nextInt(4)).associate { randomString(r) to randomValue(r, depth + 1) })
    }

    private fun randomString(r: Random): String {
        val pool = listOf("a", "Z", "é", "\u0000", "\n", "\"", "\\", "😀", "\uE000", "—", "/", "\u001f", "~")
        return (0 until r.nextInt(6)).joinToString("") { pool[r.nextInt(pool.size)] }
    }

    @Test
    fun base64url_noPadding_strict() {
        val rnd = Random(1)
        for (n in 0..70) {
            val b = ByteArray(n).also { rnd.nextBytes(it) }
            val s = Base64Url.encode(b)
            assertTrue(!s.contains('=') && !s.contains('+') && !s.contains('/'))
            assertArrayEquals(b, Base64Url.decode(s))
        }
        assertEquals("-_8", Base64Url.encode(byteArrayOf(0xFB.toByte(), 0xFF.toByte())))
        expectError("BASE64_INVALID") { Base64Url.decode("ab+c") }
        expectError("BASE64_INVALID") { Base64Url.decode("abc=") }
        expectError("BASE64_INVALID") { Base64Url.decode("a") }
        expectError("BASE64_INVALID") { Base64Url.decode("-_9") } // bits finais não nulos
    }

    @Test
    fun hex() {
        assertEquals("00ff10", Hex.encode(byteArrayOf(0, -1, 16)))
        assertArrayEquals(byteArrayOf(0, -1, 16), Hex.decode("00FF10"))
        assertTrue(Hex.isSha256Hex("a".repeat(64)))
        assertTrue(!Hex.isSha256Hex("A".repeat(64)))
    }
}
