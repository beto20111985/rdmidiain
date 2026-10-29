package br.com.rdmidiaindoor.player.config

import br.com.rdmidiaindoor.player.core.AnchorStore
import br.com.rdmidiaindoor.player.protocol.Json
import br.com.rdmidiaindoor.player.protocol.JsonValue
import br.com.rdmidiaindoor.player.protocol.ServerKey
import br.com.rdmidiaindoor.player.protocol.jsonOf
import br.com.rdmidiaindoor.player.sync.GenerationStore
import java.io.File

/**
 * Estado persistente do dispositivo (arquivo JSON com escrita atômica tmp+fsync+rename — resiste a queda de energia).
 * Não guarda segredos: a chave privada fica no Android Keystore; tokens só em memória.
 */
class DeviceStateStore(private val file: File) : AnchorStore {

    data class Identity(val deviceId: String, val screenId: String, val screenCode: String, val serverKeys: List<ServerKey>)

    private var o: MutableMap<String, JsonValue> = load()

    private fun load(): MutableMap<String, JsonValue> = try {
        if (file.isFile) LinkedHashMap(Json.parseObject(file.readBytes()).fields) else LinkedHashMap()
    } catch (e: Exception) {
        LinkedHashMap()
    }

    @Synchronized
    private fun save() {
        GenerationStore.atomicWrite(file, Json.stringify(JsonValue.Obj(o)).toByteArray())
    }

    private fun obj() = JsonValue.Obj(o)

    // ---------------- identidade ----------------
    @get:Synchronized
    val identity: Identity?
        get() {
            val id = obj().objOrNull("identity") ?: return null
            return Identity(
                id.str("device_id"), id.str("screen_id"), id.str("screen_code"),
                id.arr("server_keys").map {
                    val k = it as JsonValue.Obj
                    ServerKey(k.str("key_id"), k.str("public_key"), k.str("alg"))
                },
            )
        }

    @Synchronized
    fun setIdentity(i: Identity) {
        o["identity"] = jsonOf(
            "device_id" to i.deviceId, "screen_id" to i.screenId, "screen_code" to i.screenCode,
            "server_keys" to i.serverKeys.map { jsonOf("key_id" to it.keyId, "public_key" to it.publicKeyB64u, "alg" to it.alg) },
        )
        save()
    }

    /** Revogação/re-pareamento: remove identidade e dados ligados a ela (âncoras e kills permanecem por tela/criativo). */
    @Synchronized
    fun clearIdentity() {
        o.remove("identity")
        o.remove("pairing")
        o.remove("commands")
        save()
    }

    // ---------------- âncora (por tela) ----------------
    private fun anchors(): MutableMap<String, JsonValue> = LinkedHashMap(obj().objOrNull("anchors")?.fields ?: emptyMap())

    @Synchronized
    override fun get(screenId: String): Long? = obj().objOrNull("anchors")?.objOrNull(screenId)?.longOrNull("anchor_ms")

    @Synchronized
    override fun setIfAbsent(screenId: String, anchorMs: Long): Long {
        get(screenId)?.let { return it }
        val a = anchors()
        a[screenId] = jsonOf("anchor_ms" to anchorMs, "posted" to false)
        o["anchors"] = JsonValue.Obj(a)
        save()
        return anchorMs
    }

    @Synchronized
    override fun adoptFromServer(screenId: String, anchorMs: Long) {
        val a = anchors()
        a[screenId] = jsonOf("anchor_ms" to anchorMs, "posted" to true)
        o["anchors"] = JsonValue.Obj(a)
        save()
    }

    @Synchronized
    override fun isPostedToServer(screenId: String): Boolean =
        obj().objOrNull("anchors")?.objOrNull(screenId)?.boolOrNull("posted") ?: false

    @Synchronized
    override fun markPosted(screenId: String) {
        val cur = get(screenId) ?: return
        adoptFromServer(screenId, cur)
    }

    // ---------------- kill (persistido, com o instante de recebimento) ----------------
    /** creative_id → instante (ms) do kill. Lê também o formato antigo (lista sem instante ⇒ 0). */
    @get:Synchronized
    val kills: Map<String, Long>
        get() {
            val m = LinkedHashMap<String, Long>()
            obj().arrOrEmpty("killed").forEach { (it as? JsonValue.Str)?.value?.let { id -> m[id] = 0L } }
            obj().objOrNull("kills")?.fields?.forEach { (id, v) -> m[id] = (v as? JsonValue.Num)?.toLong() ?: 0L }
            return m
        }

    @get:Synchronized
    val killed: Set<String>
        get() = kills.keys

    private fun saveKills(m: Map<String, Long>) {
        o.remove("killed")
        o["kills"] = JsonValue.Obj(m.toSortedMap().mapValuesTo(LinkedHashMap()) { JsonValue.Num.of(it.value) })
        save()
    }

    /** Persiste ANTES de qualquer notificação (um reboot logo depois não "ressuscita" o criativo). */
    @Synchronized
    fun addKilled(ids: Collection<String>, atMs: Long): Boolean {
        val cur = kills
        val added = ids.filter { it.isNotEmpty() && it !in cur }
        if (added.isEmpty()) return false
        saveKills(cur + added.associateWith { atMs })
        return true
    }

    /**
     * Manifesto ATIVADO: libera os kills locais anteriores à geração do manifesto e ausentes do seu `revoked`
     * ([br.com.rdmidiaindoor.player.core.KillReconciler]). Devolve os creative_id liberados.
     */
    @Synchronized
    fun reconcileKills(revoked: Set<String>, generatedAtMs: Long): Set<String> {
        val cur = kills
        val lifted = br.com.rdmidiaindoor.player.core.KillReconciler.lifted(cur, revoked, generatedAtMs)
        if (lifted.isNotEmpty()) saveKills(cur - lifted)
        return lifted
    }

    // ---------------- sincronização / reprodução ----------------
    @get:Synchronized
    @set:Synchronized
    var lastSyncMs: Long?
        get() = obj().longOrNull("last_sync_ms")
        set(v) { o["last_sync_ms"] = Json.from(v); save() }

    @get:Synchronized
    @set:Synchronized
    var lastPlayMs: Long?
        get() = obj().longOrNull("last_play_ms")
        set(v) { o["last_play_ms"] = Json.from(v); save() }

    @get:Synchronized
    @set:Synchronized
    var clockOffsetMs: Long
        get() = obj().longOrNull("clock_offset_ms") ?: 0L
        set(v) { o["clock_offset_ms"] = Json.from(v); save() }

    // ---------------- comandos (idempotência na reentrega) ----------------
    @Synchronized
    fun commandStatus(commandId: String): String? = obj().objOrNull("commands")?.strOrNull(commandId)

    @Synchronized
    fun setCommandStatus(commandId: String, status: String) {
        val m = LinkedHashMap(obj().objOrNull("commands")?.fields ?: emptyMap())
        m.remove(commandId)
        m[commandId] = JsonValue.Str(status)
        while (m.size > 200) m.remove(m.keys.first())
        o["commands"] = JsonValue.Obj(m)
        save()
    }

    // ---------------- falhas (rollback por crash-loop) ----------------
    @Synchronized
    fun recordCrash(nowMs: Long, activeVersion: Long?) {
        val list = obj().arrOrEmpty("crashes").mapNotNull { it as? JsonValue.Obj }
            .filter { nowMs - (it.longOrNull("at_ms") ?: 0L) < 15 * 60_000L }
            .toMutableList()
        list += jsonOf("at_ms" to nowMs, "version" to activeVersion)
        o["crashes"] = JsonValue.Arr(list.takeLast(20))
        save()
    }

    @Synchronized
    fun recentCrashes(nowMs: Long, version: Long?, windowMs: Long): Int =
        obj().arrOrEmpty("crashes").mapNotNull { it as? JsonValue.Obj }
            .count { nowMs - (it.longOrNull("at_ms") ?: 0L) < windowMs && it.longOrNull("version") == version }

    @Synchronized
    fun clearCrashes() {
        o.remove("crashes")
        save()
    }
}
