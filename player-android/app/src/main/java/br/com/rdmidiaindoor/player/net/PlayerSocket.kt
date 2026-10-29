package br.com.rdmidiaindoor.player.net

import br.com.rdmidiaindoor.player.protocol.JsonValue
import br.com.rdmidiaindoor.player.protocol.WsEnvelope
import br.com.rdmidiaindoor.player.protocol.WsTypes
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.util.ArrayDeque

/**
 * WebSocket do player (RDPP §4): ticket de uso único (30 s) obtido com Bearer, heartbeat a cada
 * `heartbeat_interval_ms` (30 s), telemetria a cada `telemetry_interval_ms` (5 min) ou em mudança relevante,
 * reconexão com backoff exponencial + jitter (1 s → 60 s). Mensagens importantes (acks, manifest_applied/failed,
 * media_error) ficam numa fila limitada e são enviadas na reconexão.
 */
class PlayerSocket(
    private val http: OkHttpClient,
    private val scope: CoroutineScope,
    private val urlForTicket: (String) -> String,
    private val ticket: suspend () -> String,
    private val nowMs: () -> Long,
    private val listener: Listener,
) {
    interface Listener {
        fun onConnected() {}
        fun onDisconnected() {}
        fun onMessage(env: WsEnvelope) {}
        fun onAuthError(e: ApiException) {}
        fun heartbeat(): JsonValue.Obj
        fun telemetry(): JsonValue.Obj
        fun heartbeatIntervalMs(): Long = 30_000L
        fun telemetryIntervalMs(): Long = 300_000L
    }

    @Volatile
    var connected: Boolean = false
        private set

    @Volatile
    private var socket: WebSocket? = null
    private var loop: Job? = null
    private val pending = ArrayDeque<String>()

    fun start() {
        if (loop?.isActive == true) return
        loop = scope.launch { runLoop() }
    }

    fun stop() {
        loop?.cancel()
        loop = null
        socket?.close(1000, "stop")
        socket = null
        connected = false
    }

    /** Envia agora (se conectado). [important] ⇒ enfileira para a reconexão quando offline. */
    fun send(type: String, data: JsonValue.Obj, important: Boolean = false): Boolean {
        val text = WsEnvelope.of(type, data, nowMs()).encode()
        val ws = socket
        if (connected && ws != null && ws.send(text)) return true
        if (important) synchronized(pending) {
            pending.addLast(text)
            while (pending.size > 100) pending.removeFirst()
        }
        return false
    }

    fun sendTelemetryNow() {
        send(WsTypes.TELEMETRY, listener.telemetry())
    }

    private suspend fun runLoop() {
        val backoff = Backoff(1_000L, 60_000L)
        while (scope.isActive) {
            var opened = false
            try {
                val t = ticket()
                val openSignal = CompletableDeferred<Boolean>()
                val closed = CompletableDeferred<Unit>()
                val ws = http.newWebSocket(Request.Builder().url(urlForTicket(t)).build(), object : WebSocketListener() {
                    override fun onOpen(webSocket: WebSocket, response: Response) {
                        openSignal.complete(true)
                    }

                    override fun onMessage(webSocket: WebSocket, text: String) {
                        val env = try { WsEnvelope.parse(text) } catch (e: Exception) { return }
                        listener.onMessage(env)
                    }

                    override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                        webSocket.close(1000, null)
                        openSignal.complete(false)
                        closed.complete(Unit)
                    }

                    override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                        openSignal.complete(false)
                        closed.complete(Unit)
                    }

                    override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                        openSignal.complete(false)
                        closed.complete(Unit)
                    }
                })
                socket = ws
                opened = withTimeoutOrNull(20_000L) { openSignal.await() } ?: false
                if (opened) {
                    connected = true
                    backoff.reset()
                    flushPending(ws)
                    listener.onConnected()
                    val hb = scope.launch {
                        while (isActive) {
                            send(WsTypes.HEARTBEAT, listener.heartbeat())
                            delay(listener.heartbeatIntervalMs().coerceAtLeast(5_000L))
                        }
                    }
                    val tel = scope.launch {
                        while (isActive) {
                            send(WsTypes.TELEMETRY, listener.telemetry())
                            delay(listener.telemetryIntervalMs().coerceAtLeast(60_000L))
                        }
                    }
                    try {
                        closed.await()
                    } finally {
                        hb.cancel()
                        tel.cancel()
                    }
                } else {
                    ws.cancel()
                }
            } catch (e: ApiException) {
                listener.onAuthError(e)
            } catch (e: kotlinx.coroutines.CancellationException) {
                throw e
            } catch (e: Exception) {
                // rede indisponível etc. — tenta de novo com backoff
            } finally {
                val wasConnected = connected
                connected = false
                socket = null
                if (wasConnected) listener.onDisconnected()
            }
            delay(backoff.next())
        }
    }

    private fun flushPending(ws: WebSocket) {
        synchronized(pending) {
            while (pending.isNotEmpty()) {
                val text = pending.peekFirst() ?: break
                if (!ws.send(text)) break
                pending.removeFirst()
            }
        }
    }
}
