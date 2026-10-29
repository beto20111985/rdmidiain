package br.com.rdmidiaindoor.player.sync

import br.com.rdmidiaindoor.player.protocol.Hex
import br.com.rdmidiaindoor.player.protocol.Json
import br.com.rdmidiaindoor.player.protocol.JsonValue
import br.com.rdmidiaindoor.player.protocol.Manifest
import br.com.rdmidiaindoor.player.protocol.jsonOf
import java.io.File
import java.io.FileInputStream
import java.io.FileOutputStream
import java.io.IOException
import java.security.MessageDigest

data class FailedMedia(val mediaKey: String, val mediaId: String, val reason: String, val attempts: Int)

/** Estado gravado de uma geração (versão de manifesto) já verificada. */
data class GenerationState(val version: Long, val readyMediaKeys: Set<String>, val failed: List<FailedMedia>, val stagedAtMs: Long)

data class Pointer(val active: Long?, val previous: Long?, val staged: Long?)

/**
 * Armazenamento por GERAÇÕES com commit atômico (RDPP §6):
 * ```
 * <root>/media/<sha256>              mídia verificada (endereçada por conteúdo, compartilhada entre gerações)
 * <root>/media/.part/<sha256>.part   download em andamento (retomável)
 * <root>/gen/<v>.staging/            geração em montagem
 * <root>/gen/<v>/envelope.json       manifesto assinado (bytes recebidos)
 * <root>/gen/<v>/state.json          mídias prontas/falhas
 * <root>/pointer.json                {"active","previous","staged"} — troca atômica por rename
 * ```
 * Commit = reescrita atômica do ponteiro; a geração anterior é mantida para rollback; GC remove apenas o que
 * está fora de {ativa, staged, anterior} ou vencido.
 */
class GenerationStore(val root: File) {
    val mediaDir = File(root, "media")
    val partDir = File(mediaDir, ".part")
    val genDir = File(root, "gen")
    private val pointerFile = File(root, "pointer.json")

    init {
        partDir.mkdirs()
        genDir.mkdirs()
    }

    @Synchronized
    fun pointer(): Pointer {
        if (!pointerFile.isFile) return Pointer(null, null, null)
        return try {
            val o = Json.parseObject(pointerFile.readBytes())
            Pointer(o.longOrNull("active"), o.longOrNull("previous"), o.longOrNull("staged"))
        } catch (e: Exception) {
            Pointer(null, null, null)
        }
    }

    @Synchronized
    private fun writePointer(p: Pointer) {
        atomicWrite(pointerFile, Json.stringify(jsonOf("active" to p.active, "previous" to p.previous, "staged" to p.staged)).toByteArray())
    }

    fun mediaFile(sha256: String): File = File(mediaDir, sha256)
    fun partFile(sha256: String): File = File(partDir, "$sha256.part")

    /** Mídia presente com o tamanho esperado (só é renomeada para cá após o sha256 conferir). */
    fun hasVerified(sha256: String, size: Long): Boolean = mediaFile(sha256).let { it.isFile && it.length() == size }

    /** Re-hash completo (comando `redownload`): true se o arquivo confere. Leitura em stream. */
    fun reverify(sha256: String): Boolean {
        val f = mediaFile(sha256)
        if (!f.isFile) return false
        return sha256Of(f) == sha256
    }

    @Synchronized
    fun adoptVerified(part: File, sha256: String): File {
        val target = mediaFile(sha256)
        if (target.exists()) target.delete()
        if (!part.renameTo(target)) throw IOException("falha ao promover ${part.name}")
        return target
    }

    fun genPath(version: Long): File = File(genDir, version.toString())

    @Synchronized
    fun stage(version: Long, envelope: ByteArray, readyMediaKeys: Set<String>, failed: List<FailedMedia>, nowMs: Long) {
        val staging = File(genDir, "$version.staging")
        staging.deleteRecursively()
        staging.mkdirs()
        atomicWrite(File(staging, "envelope.json"), envelope)
        val state = jsonOf(
            "version" to version,
            "ready" to readyMediaKeys.sorted(),
            "failed" to failed.map { jsonOf("media_key" to it.mediaKey, "media_id" to it.mediaId, "reason" to it.reason, "attempts" to it.attempts) },
            "staged_at_ms" to nowMs,
        )
        atomicWrite(File(staging, "state.json"), Json.stringify(state).toByteArray())
        val final = genPath(version)
        val p = pointer()
        if (final.exists() && p.active != version && p.previous != version) final.deleteRecursively()
        if (final.exists()) {
            // re-stage da versão ativa (resync): substitui só o estado
            File(staging, "state.json").copyTo(File(final, "state.json"), overwrite = true)
            staging.deleteRecursively()
        } else if (!staging.renameTo(final)) {
            throw IOException("falha no rename da geração $version")
        }
        writePointer(p.copy(staged = version))
    }

    /** COMMIT: o ponteiro passa a apontar para a geração staged (troca atômica). Devolve a nova versão ativa. */
    @Synchronized
    fun commitStaged(): Long? {
        val p = pointer()
        val staged = p.staged ?: return null
        if (staged == p.active) {
            writePointer(p.copy(staged = null))
            return staged
        }
        writePointer(Pointer(active = staged, previous = p.active, staged = null))
        return staged
    }

    /** Rollback para a geração anterior (falha da nova). */
    @Synchronized
    fun rollback(): Long? {
        val p = pointer()
        val prev = p.previous ?: return null
        writePointer(Pointer(active = prev, previous = null, staged = p.staged?.takeIf { it != p.active }))
        return prev
    }

    @Synchronized
    fun clearActive() {
        writePointer(Pointer(null, null, null))
    }

    fun loadEnvelope(version: Long): ByteArray? = File(genPath(version), "envelope.json").takeIf { it.isFile }?.readBytes()

    fun loadState(version: Long): GenerationState? {
        val f = File(genPath(version), "state.json")
        if (!f.isFile) return null
        return try {
            val o = Json.parseObject(f.readBytes())
            GenerationState(
                version = o.long("version"),
                readyMediaKeys = o.arr("ready").map { (it as JsonValue.Str).value }.toSet(),
                failed = o.arrOrEmpty("failed").map {
                    val x = it as JsonValue.Obj
                    FailedMedia(x.str("media_key"), x.str("media_id"), x.str("reason"), x.int("attempts"))
                },
                stagedAtMs = o.long("staged_at_ms"),
            )
        } catch (e: Exception) {
            null
        }
    }

    data class GcReport(val removedMedia: Int, val removedGenerations: Int, val freedBytes: Long)

    /**
     * GC: remove gerações fora de {ativa, staged, anterior}; mídias não referenciadas por elas, ou cujos criativos
     * referentes estão todos vencidos em `nowMs`; parciais órfãos (sem geração em montagem que os use).
     */
    @Synchronized
    fun gc(nowMs: Long, manifestOf: (Long) -> Manifest?, inProgressShas: Set<String> = emptySet()): GcReport {
        val p = pointer()
        val keep = listOfNotNull(p.active, p.staged, p.previous).toSet()
        var removedGen = 0
        genDir.listFiles()?.forEach { f ->
            val v = f.name.toLongOrNull()
            // diretórios ".staging" remanescentes (queda no meio do stage) e gerações fora de {ativa, staged, anterior}
            if (v == null || v !in keep) {
                if (f.deleteRecursively()) removedGen++
            }
        }
        val live = HashSet<String>()
        for (v in keep) {
            val m = manifestOf(v) ?: continue
            for (media in m.media.values) {
                val users = m.creatives.values.filter { it.mediaKey == media.key }
                val usedByFallbackOrEmergency = m.fallback.any { m.creatives[it.c]?.mediaKey == media.key } ||
                    m.emergency.any { it.untilMs > nowMs && m.creatives[it.c]?.mediaKey == media.key }
                val allExpired = users.isNotEmpty() && users.all { it.validUntilMs <= nowMs }
                if (usedByFallbackOrEmergency || !allExpired) live += media.sha256
            }
        }
        live += inProgressShas
        var removed = 0
        var freed = 0L
        mediaDir.listFiles()?.forEach { f ->
            if (f.isFile && f.name !in live) {
                val len = f.length()
                if (f.delete()) { removed++; freed += len }
            }
        }
        partDir.listFiles()?.forEach { f ->
            val sha = f.name.removeSuffix(".part")
            if (sha !in live) {
                val len = f.length()
                if (f.delete()) freed += len
            }
        }
        return GcReport(removed, removedGen, freed)
    }

    /** Comando `clear_cache`: remove parciais, gerações não ativas e mídias fora da geração ativa. */
    @Synchronized
    fun clearCache(manifestOf: (Long) -> Manifest?): GcReport {
        val p = pointer()
        writePointer(Pointer(active = p.active, previous = null, staged = null))
        partDir.listFiles()?.forEach { it.delete() }
        return gc(Long.MIN_VALUE, manifestOf)
    }

    companion object {
        fun atomicWrite(target: File, data: ByteArray) {
            target.parentFile?.mkdirs()
            val tmp = File(target.parentFile, target.name + ".tmp")
            FileOutputStream(tmp).use { out ->
                out.write(data)
                out.flush()
                out.fd.sync()
            }
            if (!tmp.renameTo(target)) {
                // Em alguns sistemas o rename sobre arquivo existente falha: remove e tenta de novo.
                target.delete()
                if (!tmp.renameTo(target)) throw IOException("rename atômico falhou para ${target.name}")
            }
        }

        fun sha256Of(f: File): String {
            val md = MessageDigest.getInstance("SHA-256")
            FileInputStream(f).use { input ->
                val buf = ByteArray(64 * 1024)
                while (true) {
                    val n = input.read(buf)
                    if (n < 0) break
                    md.update(buf, 0, n)
                }
            }
            return Hex.encode(md.digest())
        }
    }
}
