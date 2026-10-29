package br.com.rdmidiaindoor.player

import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.StatFs
import android.os.SystemClock
import android.util.Log
import br.com.rdmidiaindoor.player.config.ConfigStore
import br.com.rdmidiaindoor.player.config.DeviceStateStore
import br.com.rdmidiaindoor.player.config.PinHasher
import br.com.rdmidiaindoor.player.core.AnchorManager
import br.com.rdmidiaindoor.player.core.ClockService
import br.com.rdmidiaindoor.player.core.LocalState
import br.com.rdmidiaindoor.player.core.OfflineAlerts
import br.com.rdmidiaindoor.player.net.ApiClient
import br.com.rdmidiaindoor.player.net.ApiException
import br.com.rdmidiaindoor.player.net.AuthManager
import br.com.rdmidiaindoor.player.net.Backoff
import br.com.rdmidiaindoor.player.net.PairingRequest
import br.com.rdmidiaindoor.player.net.PlayerApi
import br.com.rdmidiaindoor.player.net.PlayerSocket
import br.com.rdmidiaindoor.player.net.TelemetryCollector
import br.com.rdmidiaindoor.player.net.UrlPolicy
import br.com.rdmidiaindoor.player.ota.OtaManager
import br.com.rdmidiaindoor.player.pop.PopDatabase
import br.com.rdmidiaindoor.player.pop.PopInput
import br.com.rdmidiaindoor.player.pop.PopRecorder
import br.com.rdmidiaindoor.player.pop.PopResult
import br.com.rdmidiaindoor.player.pop.PopUploader
import br.com.rdmidiaindoor.player.pop.RoomPopStore
import br.com.rdmidiaindoor.player.protocol.AckStatus
import br.com.rdmidiaindoor.player.protocol.CommandMessage
import br.com.rdmidiaindoor.player.protocol.Commands
import br.com.rdmidiaindoor.player.protocol.DeviceSigner
import br.com.rdmidiaindoor.player.protocol.JsonValue
import br.com.rdmidiaindoor.player.protocol.KeystoreSigner
import br.com.rdmidiaindoor.player.protocol.Manifest
import br.com.rdmidiaindoor.player.protocol.ManifestCodec
import br.com.rdmidiaindoor.player.protocol.PlayerMessages
import br.com.rdmidiaindoor.player.protocol.WsEnvelope
import br.com.rdmidiaindoor.player.protocol.WsTypes
import br.com.rdmidiaindoor.player.sync.FailedMedia
import br.com.rdmidiaindoor.player.sync.GenerationStore
import br.com.rdmidiaindoor.player.sync.MediaDownloader
import br.com.rdmidiaindoor.player.sync.MediaResponse
import br.com.rdmidiaindoor.player.sync.MediaSource
import br.com.rdmidiaindoor.player.sync.SyncEngine
import br.com.rdmidiaindoor.player.sync.SyncListener
import br.com.rdmidiaindoor.player.sync.SyncOutcome
import br.com.rdmidiaindoor.player.sync.SyncState
import br.com.rdmidiaindoor.player.sync.SyncWorker
import br.com.rdmidiaindoor.player.watchdog.CrashHandler
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withTimeoutOrNull
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.io.File
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

/** Geração ativa carregada (manifesto verificado + mídias prontas). */
data class ActiveGeneration(
    val version: Long,
    val manifest: Manifest,
    val readyKeys: Set<String>,
    val failed: List<FailedMedia>,
    val store: GenerationStore,
) {
    fun mediaFile(creativeKey: String?): File? {
        val c = creativeKey?.let { manifest.creatives[it] } ?: return null
        if (c.mediaKey !in readyKeys) return null
        val m = manifest.media[c.mediaKey] ?: return null
        return store.mediaFile(m.sha256).takeIf { it.isFile }
    }
}

/**
 * Orquestrador do player (um por processo principal): identidade/pareamento, autenticação, WebSocket,
 * sincronização atômica, âncora, kill, PoP/outbox, comandos remotos, OTA e telemetria.
 * A UI ([br.com.rdmidiaindoor.player.player.PlayerActivity]) observa por [Ui] (sempre na thread principal).
 */
class PlayerRuntime(val app: PlayerApp) {

    enum class Phase { NEEDS_SETUP, PAIRING, PAIRED }

    interface Ui {
        fun onRuntimeChanged()
        fun onKill(creativeIds: Set<String>) {}
        /** [hard] = interrompe o item atual (âncora/manifesto mudou); senão só recalcula o resolvedor. */
        fun onReloadRequested(hard: Boolean) {}
    }

    val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private val main = Handler(Looper.getMainLooper())

    val config = ConfigStore(app)
    val rdRoot = File(app.filesDir, "rd")
    val state = DeviceStateStore(File(rdRoot, "state.json"))
    @Volatile private var lastPersistedOffset = state.clockOffsetMs
    val clock = ClockService(
        wallMs = { System.currentTimeMillis() },
        monotonicMs = { SystemClock.elapsedRealtime() },
        initialOffsetMs = state.clockOffsetMs,
        persist = { off -> if (kotlin.math.abs(off - lastPersistedOffset) > 1_000L) { lastPersistedOffset = off; state.clockOffsetMs = off } },
    )
    val genStore = GenerationStore(rdRoot)
    val anchors = AnchorManager(state)
    val telemetry = TelemetryCollector(app)

    val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .pingInterval(20, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    val client = ApiClient(http, { config.apiBaseUrl ?: "" }, clock)
    val api = PlayerApi(client)

    @Volatile private var signerRef: DeviceSigner? = null
    fun signer(): DeviceSigner = signerRef ?: synchronized(this) {
        signerRef ?: KeystoreSigner.getOrCreate().also { signerRef = it }
    }

    val auth = AuthManager(api, ::signer, { state.identity?.deviceId }, clock.monotonicMs, ::onDeviceRevoked)

    private val popDb by lazy { PopDatabase.open(app) }
    val popStore by lazy { RoomPopStore(popDb) }
    private val popRecorder by lazy { PopRecorder(popStore, signer()) }
    private val popUploader by lazy { PopUploader(popStore, { body -> auth.withAuth { api.postEvents(it, body) } }) }

    val ota by lazy { OtaManager(app, this) }

    @Volatile var ui: Ui? = null
    @Volatile var phase: Phase = Phase.NEEDS_SETUP
        private set
    @Volatile var pairing: PairingRequest? = null
        private set
    @Volatile var pairingError: String? = null
        private set
    @Volatile var active: ActiveGeneration? = null
        private set
    @Volatile var playerState: String = "IDLE"
    @Volatile var current: PlayerMessages.Current? = null
    @Volatile var playbackRunning: Boolean = false
    @Volatile var frozen: Boolean = false

    private val mediaSource = MediaSource { id, offset, sha ->
        val token = auth.token()
        val r: MediaResponse = client.openMedia(id, offset, sha, token)
        if (r.code == 401) auth.invalidate()
        r
    }

    val sync = SyncEngine(
        store = genStore,
        fetcher = { v -> auth.withAuth { api.manifest(it, v) } },
        downloader = MediaDownloader(genStore, mediaSource),
        serverKeys = { state.identity?.serverKeys.orEmpty() },
        expectedScreenId = { state.identity?.screenId },
        listener = object : SyncListener {
            override fun onContact(nowMs: Long) {
                state.lastSyncMs = clock.now()
                notifyUi()
            }
            override fun onRevoked(creativeIds: Set<String>, generatedAtMs: Long) = applyKill(creativeIds, generatedAtMs)
            override fun onMediaError(mediaId: String, reason: String, attempts: Int) {
                socket.send(WsTypes.MEDIA_ERROR, PlayerMessages.mediaError(mediaId, reason, attempts), important = true)
            }
            override fun onManifestFailed(version: Long?, reason: String) {
                Log.w(TAG, "manifest_failed v=$version: $reason")
                socket.send(WsTypes.MANIFEST_FAILED, PlayerMessages.manifestFailed(version, reason), important = true)
            }
            override fun onState(state: SyncState, version: Long?) {
                if (!playbackRunning) playerState = if (state == SyncState.IDLE || state == SyncState.STAGED) "IDLE" else "SYNCING"
            }
        },
        nowMs = { clock.now() },
        monoMs = clock.monotonicMs,
        freeBytes = { StatFs(rdRoot.absolutePath).availableBytes - RESERVED_BYTES },
        manifestOf = { v -> loadManifest(v) },
    )

    val socket: PlayerSocket = PlayerSocket(
        http = http,
        scope = scope,
        urlForTicket = { ticket -> UrlPolicy.wsUrl(config.apiBaseUrl ?: "", "/ws/player", mapOf("ticket" to ticket)) },
        ticket = { auth.withAuth { api.wsTicket(it) } },
        nowMs = { clock.now() },
        listener = object : PlayerSocket.Listener {
            override fun onConnected() {
                notifyUi()
                requestSync("ws_connected")
                scheduleFlush(0)
                scope.launch { postAnchorIfNeeded() }
                scope.launch { ota.checkIfDue(force = false) }
            }
            override fun onDisconnected() = notifyUi()
            override fun onMessage(env: WsEnvelope) = handleMessage(env)
            override fun onAuthError(e: ApiException) {
                if (e.isDeviceRevoked) onDeviceRevoked()
            }
            override fun heartbeat(): JsonValue.Obj = heartbeatData()
            override fun telemetry(): JsonValue.Obj = PlayerMessages.telemetry(this@PlayerRuntime.telemetry.collect(rdRoot))
            override fun heartbeatIntervalMs(): Long = active?.manifest?.settings?.heartbeatIntervalMs ?: 30_000L
            override fun telemetryIntervalMs(): Long = active?.manifest?.settings?.telemetryIntervalMs ?: 300_000L
        },
    )

    private val started = AtomicBoolean(false)
    private var pairingJob: Job? = null

    // ------------------------------------------------------------------ ciclo de vida
    fun start() {
        if (!started.compareAndSet(false, true)) return
        phase = computePhase() // síncrono: a UI já abre na fase correta
        scope.launch {
            loadActive(checkCrashLoop = true)
            evaluatePhase()
        }
    }

    private fun computePhase(): Phase = when {
        !config.isConfigured -> Phase.NEEDS_SETUP
        state.identity == null -> Phase.PAIRING
        else -> Phase.PAIRED
    }

    /** Reavalia a fase (após setup, revogação ou pareamento). */
    fun evaluatePhase() {
        phase = computePhase()
        when (phase) {
            Phase.NEEDS_SETUP -> playerState = "UNPAIRED"
            Phase.PAIRING -> {
                playerState = "UNPAIRED"
                startPairing()
            }
            Phase.PAIRED -> startOnline()
        }
        notifyUi()
    }

    private fun startOnline() {
        socket.start()
        requestSync("startup")
        scope.launch { postAnchorIfNeeded() }
        scheduleFlush(5_000)
    }

    fun notifyUi() {
        main.post { ui?.onRuntimeChanged() }
    }

    // ------------------------------------------------------------------ geração ativa
    private fun loadManifest(version: Long): Manifest? = try {
        genStore.loadEnvelope(version)?.let { ManifestCodec.parse(ManifestCodec.parseEnvelope(it).manifestJson) }
    } catch (e: Exception) {
        null
    }

    /** Carrega a geração ativa do disco; falha de carga ⇒ rollback + manifest_failed. */
    fun loadActive(checkCrashLoop: Boolean = false): ActiveGeneration? {
        var p = genStore.pointer()
        if (checkCrashLoop && p.active != null && p.previous != null &&
            state.recentCrashes(System.currentTimeMillis(), p.active, 10 * 60_000L) >= 3
        ) {
            Log.w(TAG, "crash-loop na versão ${p.active}: rollback para ${p.previous}")
            socket.send(WsTypes.MANIFEST_FAILED, PlayerMessages.manifestFailed(p.active, "CRASH_LOOP"), important = true)
            genStore.rollback()
            state.clearCrashes()
            p = genStore.pointer()
        }
        val v = p.active ?: run { active = null; return null }
        val loaded = loadGeneration(v)?.also { reconcileKills(it) }
        if (loaded == null) {
            socket.send(WsTypes.MANIFEST_FAILED, PlayerMessages.manifestFailed(v, "LOAD_FAILED"), important = true)
            genStore.rollback()?.let { prev -> active = loadGeneration(prev); return active }
            active = null
            return null
        }
        active = loaded
        return loaded
    }

    private fun loadGeneration(v: Long): ActiveGeneration? {
        val env = genStore.loadEnvelope(v) ?: return null
        val st = genStore.loadState(v) ?: return null
        return try {
            val e = ManifestCodec.parseEnvelope(env)
            // Re-verificação da assinatura na carga (defesa contra adulteração do armazenamento local)
            val keys = state.identity?.serverKeys.orEmpty()
            if (keys.isNotEmpty() && !ManifestCodec.verify(e, keys)) return null
            val m = ManifestCodec.parse(e.manifestJson)
            val ready = st.readyMediaKeys.filter { k -> m.media[k]?.let { genStore.hasVerified(it.sha256, it.size) } ?: false }.toSet()
            ActiveGeneration(v, m, ready, st.failed, genStore)
        } catch (ex: Exception) {
            null
        }
    }

    fun localState(gen: ActiveGeneration): LocalState = LocalState(state.killed, gen.readyKeys)

    fun stagedVersion(): Long? = genStore.pointer().staged?.takeIf { it != active?.version }

    /** Geração staged já carregada/verificada em background (o COMMIT na fronteira de ciclo fica barato). */
    @Volatile private var stagedCache: ActiveGeneration? = null

    /** COMMIT na fronteira de ciclo (chamado pelo controlador) ou imediatamente se nada estiver tocando. */
    @Synchronized
    fun commitStaged(): ActiveGeneration? {
        val v = genStore.commitStaged() ?: return null
        val cached = stagedCache?.takeIf { it.version == v }
        stagedCache = null
        val gen = if (cached != null) cached.also { active = it } else loadActive()
        if (gen != null) reconcileKills(gen)
        if (gen != null && gen.version == v) {
            socket.send(WsTypes.MANIFEST_APPLIED, PlayerMessages.manifestApplied(v), important = true)
            scope.launch { genStore.gc(clock.now(), ::loadManifest) }
        }
        notifyUi()
        return gen
    }

    // ------------------------------------------------------------------ sincronização
    private val syncMutex = Mutex()

    fun requestSync(reason: String, force: Boolean = false) {
        if (state.identity == null) return
        scope.launch { syncNow(reason, force) }
    }

    suspend fun syncNow(reason: String, force: Boolean = false): SyncOutcome? {
        if (state.identity == null) return null
        return syncMutex.withLock {
            Log.i(TAG, "sync ($reason, force=$force)")
            val outcome = try {
                sync.sync(force)
            } catch (e: ApiException) {
                if (e.isDeviceRevoked) onDeviceRevoked()
                SyncOutcome.Failed(e.code)
            }
            when (outcome) {
                is SyncOutcome.Staged -> {
                    stagedCache = loadGeneration(outcome.version)
                    when {
                        !playbackRunning || active == null -> commitStaged()
                        outcome.version == active?.version -> {
                            // ressincronização da MESMA versão (resync/redownload): só atualiza mídias prontas, sem interromper
                            commitStaged()
                            main.post { ui?.onReloadRequested(hard = false) }
                        }
                        else -> notifyUi()
                    }
                }
                is SyncOutcome.Failed -> if (outcome.reason.startsWith("FETCH")) SyncWorker.enqueue(app)
                else -> {}
            }
            outcome
        }
    }

    // ------------------------------------------------------------------ âncora
    /** Primeiro start válido: define a âncora local (se não houver) e agenda o POST write-once. */
    fun establishAnchor(gen: ActiveGeneration): Long {
        val a = anchors.establish(gen.manifest.screenId, gen.manifest.anchorMs, clock.now())
        scope.launch { postAnchorIfNeeded() }
        return a
    }

    fun knownAnchor(gen: ActiveGeneration): Long? = anchors.known(gen.manifest.screenId, gen.manifest.anchorMs)

    private suspend fun postAnchorIfNeeded() {
        val screen = state.identity?.screenId ?: return
        if (!anchors.needsPost(screen)) return
        val local = state.get(screen) ?: return
        try {
            val server = auth.withAuth { api.postAnchor(it, local) }
            anchors.onServerResponse(screen, server)
            if (server != local) main.post { ui?.onReloadRequested(hard = true) } // adota a âncora do servidor
        } catch (e: Exception) {
            Log.i(TAG, "POST anchor adiado: ${e.message}")
        }
    }

    // ------------------------------------------------------------------ kill / revogação
    /** Kill: persistido com o instante de recebimento e aplicado na hora (RDPP §8). */
    fun applyKill(ids: Set<String>, atMs: Long) {
        if (ids.isEmpty()) return
        val changed = state.addKilled(ids, atMs)
        if (changed) main.post { ui?.onKill(ids) }
    }

    /** Manifesto ativado: libera kills locais suspensos ("lift") — achado do E2E real do player web. */
    private fun reconcileKills(gen: ActiveGeneration) {
        val lifted = state.reconcileKills(gen.manifest.revoked, gen.manifest.generatedAtMs)
        if (lifted.isNotEmpty()) Log.i(TAG, "kill local liberado pelo manifesto v${gen.version}: $lifted")
    }

    /** Dono único do motor de reprodução no processo (evita dois laços/PoP duplicado). */
    val playbackOwner = br.com.rdmidiaindoor.player.core.SingleOwner<Any>()

    private val revoking = AtomicBoolean(false)

    /** Dispositivo revogado: para de reproduzir, apaga identidade e chave, volta ao pareamento (RDPP §3.3). */
    fun onDeviceRevoked() {
        // idempotente: sem identidade não há o que revogar (evita apagar a NOVA chave gerada para o re-pareamento)
        if (state.identity == null) return
        if (!revoking.compareAndSet(false, true)) return
        Log.w(TAG, "dispositivo revogado")
        socket.stop()
        auth.invalidate()
        state.clearIdentity()
        genStore.clearActive()
        active = null
        try { KeystoreSigner.delete() } catch (_: Exception) {}
        signerRef = null
        playbackRunning = false
        revoking.set(false)
        evaluatePhase()
    }

    // ------------------------------------------------------------------ pareamento
    private fun deviceInfo(): Map<String, String> {
        val (w, h) = telemetry.resolution()
        return mapOf(
            "model" to "${Build.MANUFACTURER} ${Build.MODEL}",
            "os" to "Android ${Build.VERSION.RELEASE} (API ${Build.VERSION.SDK_INT})",
            "resolution" to "${w}x$h",
        )
    }

    private fun startPairing() {
        if (pairingJob?.isActive == true) return
        pairingJob = scope.launch {
            val backoff = Backoff(2_000L, 60_000L)
            while (isActive && state.identity == null) {
                try {
                    val req = api.createPairingRequest(signer().publicKeyB64u(), BuildConfig.VERSION_NAME, deviceInfo())
                    pairing = req
                    pairingError = null
                    notifyUi()
                    backoff.reset()
                    if (waitForClaim(req)) {
                        val result = api.pairingResult(req.requestId, signer(), clock.now())
                        state.setIdentity(DeviceStateStore.Identity(result.deviceId, result.screenId, result.screenCode, result.serverKeys))
                        pairing = null
                        evaluatePhase()
                        return@launch
                    }
                } catch (e: kotlinx.coroutines.CancellationException) {
                    throw e
                } catch (e: Exception) {
                    pairingError = e.message
                    notifyUi()
                    delay(backoff.next())
                }
            }
        }
    }

    /** WS de pareamento (sem segredo) com fallback para GET de status com backoff ≥ 5 s, máx. 60 s, jitter. */
    private suspend fun waitForClaim(req: PairingRequest): Boolean {
        val backoff = Backoff(5_000L, 60_000L)
        while (clock.now() < req.expiresAtMs) {
            val viaWs = withTimeoutOrNull((req.expiresAtMs - clock.now()).coerceAtLeast(1_000L)) { waitPairingWs(req) }
            when (viaWs) {
                true -> return true
                false -> return false
                null -> {}
            }
            try {
                when (api.pairingStatus(req.requestId)) {
                    "CLAIMED" -> return true
                    "EXPIRED" -> return false
                }
            } catch (e: ApiException) {
                if (e.httpStatus == 404 || e.httpStatus == 410) return false
            } catch (_: Exception) {}
            delay(backoff.next())
        }
        return false
    }

    /** true=claimed, false=expired, null=WS indisponível/fechado. */
    private suspend fun waitPairingWs(req: PairingRequest): Boolean? {
        val result = kotlinx.coroutines.CompletableDeferred<Boolean?>()
        val url = try {
            UrlPolicy.wsUrl(config.apiBaseUrl ?: return null, req.wsPath ?: "/ws/player/pairing", mapOf("request_id" to req.requestId))
        } catch (e: Exception) { return null }
        val ws = http.newWebSocket(Request.Builder().url(url).build(), object : WebSocketListener() {
            override fun onMessage(webSocket: WebSocket, text: String) {
                val env = try { WsEnvelope.parse(text) } catch (e: Exception) { return }
                when (env.type) {
                    WsTypes.PAIRING_CLAIMED -> result.complete(true)
                    WsTypes.PAIRING_EXPIRED -> result.complete(false)
                }
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) { result.complete(null) }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) { result.complete(null) }
        })
        return try { result.await() } finally { ws.cancel() }
    }

    /** 2.3b — técnico no dispositivo com login controlado (credenciais nunca armazenadas). */
    suspend fun claimWithLogin(login: String, password: String, totp: String?, screenCode: String) {
        val req = pairing ?: throw IllegalStateException("sem pedido de pareamento ativo")
        api.claim(req.requestId, login, password, totp, screenCode)
    }

    // ------------------------------------------------------------------ PoP
    fun recordPop(input: PopInput) {
        val id = state.identity ?: return
        scope.launch(Dispatchers.IO) {
            try {
                popRecorder.record(id.deviceId, id.screenId, input)
                if (input.result == PopResult.COMPLETED) state.lastPlayMs = input.finishedAtMs
                scheduleFlush(3_000)
            } catch (e: Exception) {
                Log.e(TAG, "falha ao gravar PoP", e)
            }
        }
    }

    private var flushJob: Job? = null

    @Synchronized
    fun scheduleFlush(delayMs: Long) {
        if (flushJob?.isActive == true) return
        flushJob = scope.launch(Dispatchers.IO) {
            delay(delayMs)
            flushEvents()
        }
    }

    suspend fun flushEvents() {
        if (state.identity == null) return
        try {
            popUploader.flush()
        } catch (e: ApiException) {
            if (e.isDeviceRevoked) onDeviceRevoked()
        } catch (e: Exception) {
            Log.i(TAG, "envio de PoP adiado: ${e.message}")
        }
    }

    // ------------------------------------------------------------------ WS: mensagens do servidor
    private fun handleMessage(env: WsEnvelope) {
        when (env.type) {
            WsTypes.SERVER_TIME -> env.data.longOrNull("server_time_ms")?.let {
                val now = clock.localNow()
                clock.onServerTime(it, now, now)
            }
            WsTypes.SYNC_REQUIRED -> requestSync("sync_required")
            WsTypes.FLUSH_EVENTS -> scheduleFlush(0)
            WsTypes.KILL -> {
                val ids = env.data.arrOrEmpty("creative_ids").mapNotNull { (it as? JsonValue.Str)?.value }.toSet()
                // instante do servidor (envelope) quando houver: comparável ao generated_at_ms dos manifestos
                applyKill(ids, env.tsMs.takeIf { it > 0 } ?: clock.now())
            }
            WsTypes.DEVICE_REVOKED -> onDeviceRevoked()
            WsTypes.COMMAND -> try {
                handleCommand(CommandMessage.from(env.data))
            } catch (e: Exception) {
                Log.w(TAG, "comando inválido: ${e.message}")
            }
        }
    }

    private fun ack(id: String, status: AckStatus, detail: String? = null) {
        if (status != AckStatus.RECEIVED) state.setCommandStatus(id, status.name)
        socket.send(WsTypes.COMMAND_ACK, PlayerMessages.commandAck(id, status, detail), important = status != AckStatus.RECEIVED)
    }

    /** Comandos persistidos no servidor e reentregues até o ack final: execução idempotente por command_id. */
    fun handleCommand(cmd: CommandMessage) {
        val previous = state.commandStatus(cmd.commandId)
        if (previous != null && previous != AckStatus.RECEIVED.name) {
            ack(cmd.commandId, AckStatus.valueOf(previous), "reentrega")
            return
        }
        if (cmd.expiresAtMs != null && clock.now() > cmd.expiresAtMs) {
            ack(cmd.commandId, AckStatus.FAILED, "EXPIRED")
            return
        }
        ack(cmd.commandId, AckStatus.RECEIVED)
        state.setCommandStatus(cmd.commandId, AckStatus.RECEIVED.name)
        scope.launch {
            try {
                when (cmd.command) {
                    Commands.RESYNC -> {
                        val o = syncNow("command", force = true)
                        ack(cmd.commandId, if (o is SyncOutcome.Failed) AckStatus.FAILED else AckStatus.DONE, (o as? SyncOutcome.Failed)?.reason)
                    }
                    Commands.CLEAR_CACHE -> {
                        val r = genStore.clearCache(::loadManifest)
                        app.cacheDir.listFiles()?.forEach { it.deleteRecursively() }
                        ack(cmd.commandId, AckStatus.DONE, "removidos=${r.removedMedia} liberados=${r.freedBytes}")
                        requestSync("clear_cache", force = true)
                    }
                    Commands.REDOWNLOAD -> {
                        val gen = active
                        var bad = 0
                        gen?.manifest?.media?.values?.distinctBy { it.sha256 }?.forEach { m ->
                            if (genStore.mediaFile(m.sha256).isFile && !genStore.reverify(m.sha256)) {
                                genStore.mediaFile(m.sha256).delete(); bad++
                            }
                        }
                        val o = syncNow("redownload", force = true)
                        ack(cmd.commandId, if (o is SyncOutcome.Failed) AckStatus.FAILED else AckStatus.DONE, "corrompidas=$bad")
                    }
                    Commands.RELOAD_MANIFEST -> {
                        loadActive()
                        main.post { ui?.onReloadRequested(hard = true) }
                        ack(cmd.commandId, AckStatus.DONE)
                    }
                    Commands.UPDATE_APP -> {
                        val r = ota.checkIfDue(force = true)
                        ack(cmd.commandId, if (r.ok) AckStatus.DONE else AckStatus.FAILED, r.detail)
                    }
                    Commands.RESTART_APP -> {
                        ack(cmd.commandId, AckStatus.DONE)
                        delay(1_500)
                        CrashHandler.restartProcess(app, "command")
                    }
                    Commands.REBOOT -> {
                        if (!app.kiosk.isDeviceOwner) {
                            ack(cmd.commandId, AckStatus.UNSUPPORTED, "requer Device Owner")
                        } else {
                            ack(cmd.commandId, AckStatus.DONE)
                            delay(1_500)
                            if (!app.kiosk.reboot()) Log.w(TAG, "reboot falhou")
                        }
                    }
                    Commands.SET_TECH_PIN -> {
                        // RDPP §4: {"algo":"pbkdf2-sha256","iterations":210000,"salt_b64u","hash_b64u"} — nunca o PIN em claro
                        val a = cmd.args
                        val hash = PinHasher.fromCommandArgs(
                            a.strOrNull("algo"), a.longOrNull("iterations"), a.strOrNull("salt_b64u"), a.strOrNull("hash_b64u"),
                        )
                        if (hash != null) {
                            config.techPinHash = hash
                            config.pinFailures = 0
                            ack(cmd.commandId, AckStatus.DONE)
                        } else ack(cmd.commandId, AckStatus.FAILED, "INVALID_ARGS")
                    }
                    else -> ack(cmd.commandId, AckStatus.UNSUPPORTED, "comando desconhecido")
                }
            } catch (e: Exception) {
                ack(cmd.commandId, AckStatus.FAILED, e.message?.take(200))
            }
        }
    }

    // ------------------------------------------------------------------ heartbeat / alertas
    @Volatile private var usedBytesCache = 0L
    @Volatile private var usedBytesAt = 0L

    private fun usedBytes(): Long {
        val now = SystemClock.elapsedRealtime()
        if (now - usedBytesAt > 300_000L || usedBytesAt == 0L) {
            usedBytesCache = rdRoot.walkTopDown().filter { it.isFile }.sumOf { it.length() }
            usedBytesAt = now
        }
        return usedBytesCache
    }

    fun alerts(): List<String> {
        val out = ArrayList<String>()
        val gen = active
        when (OfflineAlerts.level(clock.now(), state.lastSyncMs, gen?.manifest?.settings?.offlineAlertDays ?: listOf(7, 28))) {
            OfflineAlerts.Level.FOOTER -> out += "OFFLINE_7D"
            OfflineAlerts.Level.CENTER -> out += "OFFLINE_28D"
            else -> {}
        }
        if (clock.clockAlert(gen?.manifest?.generatedAtMs)) out += "CLOCK_INVALID"
        if (gen != null && gen.failed.isNotEmpty()) out += "MEDIA_ERROR"
        if (frozen) out += "FROZEN"
        return out
    }

    private fun heartbeatData(): JsonValue.Obj {
        val p = genStore.pointer()
        val downloading = sync.state == SyncState.DOWNLOADING
        val stat = StatFs(rdRoot.absolutePath)
        return PlayerMessages.heartbeat(
            state = if (frozen) "FROZEN" else playerState,
            current = current,
            manifestVersionApplied = active?.version,
            manifestVersionPending = p.staged?.takeIf { it != active?.version },
            download = if (downloading) PlayerMessages.Download(sync.doneBytes.get(), sync.totalBytes.get()) else null,
            lastSyncMs = state.lastSyncMs,
            lastPlayMs = state.lastPlayMs,
            storage = PlayerMessages.Storage(stat.availableBytes, usedBytes(), true),
            clockOffsetMs = clock.offsetMs,
            alerts = alerts(),
        )
    }

    /** Online para o PoP: WS conectado ou contato HTTP com o servidor nos últimos 90 s (achado do E2E web). */
    fun isOnline(): Boolean = socket.connected || clock.contactedWithin(ONLINE_WINDOW_MS)

    /** Troca de URL base pelo técnico ⇒ nova instalação lógica: limpa identidade e volta ao pareamento. */
    fun changeApiBaseUrl(url: String) {
        config.apiBaseUrl = url
        if (state.identity != null) onDeviceRevoked() else evaluatePhase()
    }

    companion object {
        private const val TAG = "RdRuntime"
        private const val RESERVED_BYTES = 200L * 1024 * 1024
        const val ONLINE_WINDOW_MS = 90_000L
    }
}
