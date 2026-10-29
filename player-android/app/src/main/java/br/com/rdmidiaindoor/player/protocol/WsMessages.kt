package br.com.rdmidiaindoor.player.protocol

import java.util.UUID

/** Envelope WS do RDPP §4: `{"type","id","ts_ms","data"}` (tipos em minúsculas com `_`). */
data class WsEnvelope(val type: String, val id: String, val tsMs: Long, val data: JsonValue.Obj) {
    fun encode(): String = Json.stringify(jsonOf("type" to type, "id" to id, "ts_ms" to tsMs, "data" to data))

    companion object {
        fun of(type: String, data: JsonValue.Obj, tsMs: Long, id: String = UUID.randomUUID().toString()) =
            WsEnvelope(type, id, tsMs, data)

        fun parse(text: String): WsEnvelope {
            val o = Json.parseObject(text)
            return WsEnvelope(
                type = o.str("type"),
                id = o.strOrNull("id") ?: "",
                tsMs = o.longOrNull("ts_ms") ?: 0L,
                data = o.objOrNull("data") ?: JsonValue.Obj(emptyMap()),
            )
        }
    }
}

object WsTypes {
    // servidor → player
    const val SERVER_TIME = "server_time"
    const val SYNC_REQUIRED = "sync_required"
    const val COMMAND = "command"
    const val KILL = "kill"
    const val DEVICE_REVOKED = "device_revoked"
    const val FLUSH_EVENTS = "flush_events"
    const val PAIRING_CLAIMED = "pairing_claimed"
    const val PAIRING_EXPIRED = "pairing_expired"

    // player → servidor
    const val HEARTBEAT = "heartbeat"
    const val TELEMETRY = "telemetry"
    const val COMMAND_ACK = "command_ack"
    const val MANIFEST_APPLIED = "manifest_applied"
    const val MANIFEST_FAILED = "manifest_failed"
    const val MEDIA_ERROR = "media_error"
}

/** Comandos do RDPP §4 (`set_tech_pin` normatizado em 2026-10-07). */
object Commands {
    const val RESTART_APP = "restart_app"
    const val RESYNC = "resync"
    const val CLEAR_CACHE = "clear_cache"
    const val REDOWNLOAD = "redownload"
    const val UPDATE_APP = "update_app"
    const val REBOOT = "reboot"
    const val RELOAD_MANIFEST = "reload_manifest"
    const val SET_TECH_PIN = "set_tech_pin"

    val STANDARD = setOf(RESTART_APP, RESYNC, CLEAR_CACHE, REDOWNLOAD, UPDATE_APP, REBOOT, RELOAD_MANIFEST, SET_TECH_PIN)
}

enum class AckStatus { RECEIVED, DONE, FAILED, UNSUPPORTED }

data class CommandMessage(val commandId: String, val command: String, val args: JsonValue.Obj, val expiresAtMs: Long?) {
    companion object {
        fun from(data: JsonValue.Obj) = CommandMessage(
            commandId = data.str("command_id"),
            command = data.str("command"),
            args = data.objOrNull("args") ?: JsonValue.Obj(emptyMap()),
            expiresAtMs = data.longOrNull("expires_at_ms"),
        )
    }
}

/** Construtores das mensagens player → servidor (todas as chaves presentes, conforme §4). */
object PlayerMessages {
    data class Current(val creativeCode: String, val startedAtMs: Long)
    data class Download(val doneBytes: Long, val totalBytes: Long)
    data class Storage(val freeBytes: Long, val usedBytes: Long, val persistent: Boolean)

    fun heartbeat(
        state: String,
        current: Current?,
        manifestVersionApplied: Long?,
        manifestVersionPending: Long?,
        download: Download?,
        lastSyncMs: Long?,
        lastPlayMs: Long?,
        storage: Storage,
        clockOffsetMs: Long,
        alerts: List<String>,
    ): JsonValue.Obj = jsonOf(
        "state" to state,
        "current" to current?.let { jsonOf("creative_code" to it.creativeCode, "started_at_ms" to it.startedAtMs) },
        "manifest_version_applied" to manifestVersionApplied,
        "manifest_version_pending" to manifestVersionPending,
        "download" to download?.let { jsonOf("done_bytes" to it.doneBytes, "total_bytes" to it.totalBytes) },
        "last_sync_ms" to lastSyncMs,
        "last_play_ms" to lastPlayMs,
        "storage" to jsonOf("free_bytes" to storage.freeBytes, "used_bytes" to storage.usedBytes, "persistent" to storage.persistent),
        "clock_offset_ms" to clockOffsetMs,
        "alerts" to alerts,
    )

    data class Telemetry(
        val cpuPct: Long?,
        val ramUsedMb: Long,
        val ramTotalMb: Long,
        val diskFreeMb: Long,
        val diskTotalMb: Long,
        val uptimeS: Long,
        val os: String,
        val appVersion: String,
        val platform: String,
        val resolution: String,
        val orientation: String,
        val wifiSsid: String?,
        val wifiRssiDbm: Long?,
        val hdmiConnected: Boolean?,
    )

    /** Telemetria SEM IP (ETAPA2 #315–#316). */
    fun telemetry(t: Telemetry): JsonValue.Obj = jsonOf(
        "cpu_pct" to t.cpuPct,
        "ram_used_mb" to t.ramUsedMb,
        "ram_total_mb" to t.ramTotalMb,
        "disk_free_mb" to t.diskFreeMb,
        "disk_total_mb" to t.diskTotalMb,
        "uptime_s" to t.uptimeS,
        "os" to t.os,
        "app_version" to t.appVersion,
        "platform" to t.platform,
        "resolution" to t.resolution,
        "orientation" to t.orientation,
        "wifi" to if (t.wifiSsid == null && t.wifiRssiDbm == null) null else jsonOf("ssid" to t.wifiSsid, "rssi_dbm" to t.wifiRssiDbm),
        "hdmi_connected" to t.hdmiConnected,
    )

    fun commandAck(commandId: String, status: AckStatus, detail: String?) =
        jsonOf("command_id" to commandId, "status" to status.name, "detail" to detail)

    fun manifestApplied(version: Long) = jsonOf("version" to version)
    fun manifestFailed(version: Long?, reason: String) = jsonOf("version" to version, "reason" to reason)
    fun mediaError(mediaId: String, reason: String, attempts: Int) = jsonOf("media_id" to mediaId, "reason" to reason, "attempts" to attempts)
}
