package br.com.rdmidiaindoor.player.player

import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.util.Log
import br.com.rdmidiaindoor.player.ActiveGeneration
import br.com.rdmidiaindoor.player.PlayerRuntime
import br.com.rdmidiaindoor.player.core.CycleResolver
import br.com.rdmidiaindoor.player.core.Decision
import br.com.rdmidiaindoor.player.core.PlaybackPlanner
import br.com.rdmidiaindoor.player.core.PlaybackTiming
import br.com.rdmidiaindoor.player.core.PopChain
import br.com.rdmidiaindoor.player.core.Segment
import br.com.rdmidiaindoor.player.core.SegmentKind
import br.com.rdmidiaindoor.player.pop.PopInput
import br.com.rdmidiaindoor.player.pop.PopResult
import br.com.rdmidiaindoor.player.pop.PopRules
import br.com.rdmidiaindoor.player.protocol.Manifest
import br.com.rdmidiaindoor.player.protocol.ManifestCodec
import br.com.rdmidiaindoor.player.protocol.PlayerMessages
import br.com.rdmidiaindoor.player.watchdog.PlaybackWatchdog

/**
 * Motor de reprodução (thread principal): a cada fronteira consulta o [PlaybackPlanner] (tempo absoluto corrigido),
 * troca as camadas do [Stage] com fade, mede o PoP com relógio MONOTÔNICO (início = instante em que o item fica
 * visível; fim = instante em que o próximo fica visível — intervalos contíguos, sem sobreposição; §8.2.10),
 * pré-carrega o próximo item e aplica o COMMIT de manifesto staged na fronteira de ciclo (§5.1 item 8).
 */
class PlayerController(
    private val runtime: PlayerRuntime,
    private val stage: Stage,
    private val onShowing: (label: String, timezone: String?) -> Unit,
    private val watchdog: PlaybackWatchdog,
) {
    private val handler = Handler(Looper.getMainLooper())
    private var gen: ActiveGeneration? = null
    private var planner: PlaybackPlanner? = null
    private var showing: Showing? = null
    private var pendingSwitch: Decision? = null
    private var running = false
    private var stageW = 1920
    private var stageH = 1080

    private class Showing(
        val decision: Decision,
        val creative: Manifest.CreativeRef?,
        val manifestVersion: Long,
        val hasMedia: Boolean,
        val isVideo: Boolean,
        val monoStart: Long,
        /** Instante corrigido REAL em que ficou visível (base do agendamento). */
        val showWall: Long,
        /** Início registrado no PoP (= showWall, ou deslocado para não sobrepor o registro anterior — [PopChain]). */
        val popStart: Long,
        val offsetAtStart: Long,
        val offlineAtStart: Boolean,
    ) {
        /** ms descontados do fim do registro (sobreposição criada por correção do relógio para trás). */
        var trimEndMs = 0L
        var videoEnded = false
        var targetReached = false
        var error = false
        var interrupted = false
        var lastVideoPos = -1L
    }

    private val tickRunnable = Runnable { tick() }
    private val targetRunnable = Runnable { onTargetReached() }
    /** Emergência que começa no meio do item atual preempta na hora (§5.1 item 7). */
    private val emergencyRunnable = Runnable {
        showing?.interrupted = true
        tick()
    }
    private val progressRunnable = object : Runnable {
        override fun run() {
            reportProgress()
            handler.postDelayed(this, 2_000L)
        }
    }

    val isRunning: Boolean get() = running

    fun setStageSize(w: Int, h: Int) {
        if (w > 0 && h > 0) { stageW = w; stageH = h }
    }

    fun start() {
        if (running) return
        // instância única do motor no processo: quem assume para o anterior (Activity recriada, CLEAR_TASK etc.)
        (runtime.playbackOwner.claim(this) as? PlayerController)?.stop()
        running = true
        runtime.playbackRunning = true
        watchdog.onFrozen = { restartEngine() }
        handler.post(progressRunnable)
        rebuild()
        tick()
    }

    fun stop() {
        if (!running) return
        running = false
        handler.removeCallbacksAndMessages(null)
        finishCurrent(SystemClock.elapsedRealtime(), interrupted = true)
        showing = null
        pendingSwitch = null
        if (runtime.playbackOwner.isOwner(this)) {
            runtime.playbackRunning = false
            runtime.current = null
        }
        runtime.playbackOwner.release(this)
        watchdog.nextSwitchDueElapsed = Long.MAX_VALUE
    }

    /** (Re)constrói o resolvedor a partir da geração ativa + estado local (kills, mídias prontas). */
    fun rebuild(): Boolean {
        val g = runtime.active ?: runtime.loadActive()
        gen = g
        if (g == null) {
            planner = null
            return false
        }
        val anchor = runtime.knownAnchor(g) ?: if (g.readyKeys.isNotEmpty()) runtime.establishAnchor(g) else null
        if (anchor == null) {
            planner = null
            return false
        }
        val resolver = CycleResolver(g.manifest, anchor, runtime.localState(g))
        val p = planner
        if (p == null) planner = PlaybackPlanner(resolver) else p.replaceResolver(resolver)
        return true
    }

    fun onKill(ids: Set<String>) {
        if (!running) return
        rebuild()
        val cur = showing
        if (cur?.creative != null && cur.creative.creativeId in ids) {
            Log.w(TAG, "kill do criativo em exibição ${cur.creative.code}: interrompendo")
            cur.interrupted = true
            scheduleTick(0)
        } else {
            // a pré-carga pode estar desatualizada: a próxima decisão recalcula
        }
    }

    fun reload() {
        if (!running) return
        rebuild()
        showing?.interrupted = true
        scheduleTick(0)
    }

    private fun restartEngine() {
        Log.w(TAG, "watchdog: reiniciando motor de reprodução")
        showing?.error = true
        stage.releaseAll()
        showing = null
        rebuild()
        scheduleTick(0)
    }

    private fun scheduleTick(delayMs: Long) {
        handler.removeCallbacks(tickRunnable)
        handler.removeCallbacks(targetRunnable)
        handler.removeCallbacks(emergencyRunnable)
        handler.postDelayed(tickRunnable, delayMs)
    }

    /** Recalcula o resolvedor (ex.: novas mídias prontas) sem interromper o item em exibição. */
    fun refresh() {
        if (running) rebuild()
    }

    private fun tick() {
        if (!running) return
        if (!runtime.playbackOwner.isOwner(this)) {
            stop()
            return
        }
        handler.removeCallbacks(targetRunnable)
        handler.removeCallbacks(emergencyRunnable)
        // Relógio corrigido para TRÁS durante o item (achado do E2E real): o fim agendado chegou antes da fronteira
        // absoluta. Em vez de um FILLER relâmpago, o item atual continua até a fronteira (o excesso é descontado do
        // registro pelo PopChain e a grade volta a ficar alinhada).
        val cur = showing
        if (cur != null && pendingSwitch == null && !cur.interrupted && !cur.error) {
            val remaining = PlaybackTiming.extensionMs(cur.decision.segment.endMs, runtime.clock.now())
            if (remaining != null) {
                watchdog.nextSwitchDueElapsed = SystemClock.elapsedRealtime() + remaining + 5_000L
                handler.postDelayed(targetRunnable, remaining)
                return
            }
        }
        var p = planner
        if (p == null) {
            // nada tocando: uma geração staged pode ser ativada imediatamente
            if (runtime.stagedVersion() != null) runtime.commitStaged()
            if (!rebuild()) {
                showPlaceholder()
                scheduleTick(5_000)
                return
            }
        }
        p = planner ?: return
        val now = runtime.clock.now()

        // COMMIT do manifesto staged somente na fronteira de ciclo (fase == 0, dentro da tolerância de troca)
        if (runtime.stagedVersion() != null) {
            val r = p.resolver
            val cycleStart = r.cycleStart(r.cycleIndex(now))
            if (now - cycleStart in 0..p.joinToleranceMs) {
                if (runtime.commitStaged() != null) {
                    rebuild()
                    p = planner ?: return
                }
            }
        }

        val d = p.decide(now)
        for (s in d.skipped) recordSkipped(s, now)
        val g = gen ?: return
        val item = displayItemFor(d.segment, g)
        if (d.segment.kind == SegmentKind.ITEM) p.markStarted(d.segment)
        pendingSwitch = d
        val front = stage.front
        if (d.segment.kind == SegmentKind.FILLER && front.loadedId == item.id && front.ready) {
            // FILLER do mesmo criativo (ex.: última repetição incompleta da emergência): mantém imagem/último quadro
            continueOnFront(d, g)
            return
        }
        if (stage.back.loadedId == item.id && stage.back.ready) {
            doSwap(d, item, g)
        } else {
            stage.back.prepare(item, stageW, stageH,
                onReady = { if (pendingSwitch === d) doSwap(d, item, g) },
                onPrepareError = { reason ->
                    Log.w(TAG, "falha ao preparar ${item.id}: $reason")
                    if (pendingSwitch === d) {
                        // mostra a tela institucional no lugar (sem PoP) até o fim do segmento
                        val ph = DisplayItem("ph:${d.segment.startMs}", null, false, null)
                        stage.back.prepare(ph, stageW, stageH, onReady = { doSwap(d.copy(recordPop = false), ph, g) }, onPrepareError = {})
                    }
                },
            )
        }
        watchdog.nextSwitchDueElapsed = SystemClock.elapsedRealtime() + 15_000L // preparação não pode travar
    }

    private fun continueOnFront(d: Decision, g: ActiveGeneration) {
        pendingSwitch = null
        switchAt(d, g, isVideo = false, hasMedia = true)
    }

    /** Fecha o item anterior e abre o novo no MESMO instante (PoP contíguo, sem sobreposição — §8.2.10). */
    private fun switchAt(d: Decision, g: ActiveGeneration, isVideo: Boolean, hasMedia: Boolean) {
        val mono = SystemClock.elapsedRealtime()
        val showWall = runtime.clock.now()
        val prev = showing
        var popStart = showWall
        if (prev != null && isPopRecord(prev)) {
            val link = PopChain.link(prev.popStart, mono - prev.monoStart, prev.decision.expectedMs, showWall)
            prev.trimEndMs = link.trimPreviousMs
            popStart = link.popStartMs
        }
        finishCurrent(mono, interrupted = false)
        beginShowing(d, g, mono, showWall, popStart, isVideo, hasMedia)
    }

    private fun isPopRecord(s: Showing): Boolean =
        s.decision.recordPop && s.hasMedia && s.creative != null && s.decision.segment.kind == SegmentKind.ITEM

    private fun doSwap(d: Decision, item: DisplayItem, g: ActiveGeneration) {
        if (!running) return
        pendingSwitch = null
        val fade = g.manifest.settings.fadeMs.coerceIn(0, 2_000)
        stage.back.onEnded = { onVideoEnded(item.id) }
        stage.back.onError = { reason -> onPlaybackError(item.id, reason) }
        stage.swap(fade, onSwapped = {
            switchAt(d, g, isVideo = item.isVideo && item.file != null, hasMedia = item.file != null)
        }, onFadeDone = { preloadNext(d, g) })
    }

    private fun beginShowing(
        d: Decision, g: ActiveGeneration, mono: Long, corrected: Long, popStart: Long, isVideo: Boolean, hasMedia: Boolean,
    ) {
        run {
            val creative = d.segment.creativeKey?.let { g.manifest.creatives[it] }
            showing = Showing(
                decision = d,
                creative = creative,
                manifestVersion = g.version,
                hasMedia = hasMedia,
                isVideo = isVideo,
                monoStart = mono,
                showWall = corrected,
                popStart = popStart,
                offsetAtStart = runtime.clock.offsetMs,
                offlineAtStart = !runtime.isOnline(),
            )
            onShowing(footerText(creative), g.manifest.timezone)
            runtime.playerState = "PLAYING"
            runtime.current = creative?.let { PlayerMessages.Current(it.code, corrected) }
            watchdog.lastProgressElapsed = mono
            val delay = (d.targetEndMs - corrected).coerceAtLeast(0L)
            watchdog.nextSwitchDueElapsed = mono + delay + (if (isVideo) 5_000L else 0L)
            handler.postDelayed(targetRunnable, delay)
            // emergência que começa antes do fim-alvo preempta o item atual
            g.manifest.emergency.map { it.fromMs }.filter { it > corrected && it < d.targetEndMs }.minOrNull()?.let {
                handler.postDelayed(emergencyRunnable, it - corrected)
            }
        }
    }

    private fun onPlaybackError(itemId: String, reason: String) {
        val s = showing ?: return
        if (stage.front.loadedId != itemId) return
        Log.w(TAG, "erro de reprodução em $itemId: $reason")
        s.error = true
        scheduleTick(0)
    }

    private fun preloadNext(d: Decision, g: ActiveGeneration) {
        val p = planner ?: return
        if (!running || pendingSwitch != null) return
        val next = try { p.peek(d.targetEndMs) } catch (e: Exception) { return }
        val item = displayItemFor(next, g)
        if (stage.back.loadedId == item.id) return
        stage.back.prepare(item, stageW, stageH, onReady = {}, onPrepareError = {})
    }

    private fun onTargetReached() {
        val s = showing ?: return scheduleTick(0)
        s.targetReached = true
        if (s.isVideo && !s.videoEnded && !s.error) {
            // vídeo nunca é cortado: espera o fim do decodificador (tolerância do gerador: +40 ms)
            handler.postDelayed({
                val cur = showing
                if (cur === s && !s.videoEnded) {
                    Log.w(TAG, "vídeo não terminou 5 s após o alvo: encerrando como erro")
                    s.error = true
                    tick()
                }
            }, 5_000L)
            return
        }
        tick()
    }

    private fun onVideoEnded(itemId: String) {
        val s = showing ?: return
        if (stage.front.loadedId != itemId) return
        s.videoEnded = true
        if (s.targetReached) scheduleTick(0)
    }

    private fun finishCurrent(monoNow: Long, interrupted: Boolean) {
        val s = showing ?: return
        showing = null
        val d = s.decision
        if (!d.recordPop || !s.hasMedia || s.creative == null || d.segment.kind != SegmentKind.ITEM) return
        val actual = (monoNow - s.monoStart - s.trimEndMs).coerceAtLeast(0L)
        val endedNaturally = s.targetReached && (!s.isVideo || s.videoEnded)
        val result = PopRules.result(d.expectedMs, actual, endedNaturally, interrupted || s.interrupted, s.error)
        runtime.recordPop(
            PopInput(
                creativeId = s.creative.creativeId,
                creativeCode = s.creative.code,
                campaignId = s.creative.campaignId,
                insertionId = d.segment.insertionId,
                kind = s.creative.kind,
                manifestVersion = s.manifestVersion,
                startedAtMs = s.popStart,
                expectedMs = d.expectedMs,
                actualMs = actual,
                result = result,
                clockOffsetMs = s.offsetAtStart,
                offline = s.offlineAtStart,
            ),
        )
    }

    /** SKIPPED: started = finished = instante corrigido da detecção, actual 0, expected = d (§8.2.9). */
    private fun recordSkipped(seg: Segment, now: Long) {
        val g = gen ?: return
        val c = seg.creativeKey?.let { g.manifest.creatives[it] } ?: return
        runtime.recordPop(
            PopInput(
                creativeId = c.creativeId, creativeCode = c.code, campaignId = c.campaignId, insertionId = seg.insertionId,
                kind = c.kind, manifestVersion = g.version, startedAtMs = now, expectedMs = seg.durationMs, actualMs = 0,
                result = PopResult.SKIPPED, clockOffsetMs = runtime.clock.offsetMs, offline = !runtime.isOnline(),
            ),
        )
    }

    private fun displayItemFor(seg: Segment, g: ActiveGeneration): DisplayItem {
        val file = g.mediaFile(seg.creativeKey)
        val media = seg.creativeKey?.let { g.manifest.creatives[it] }?.let { g.manifest.media[it.mediaKey] }
        val id = "${seg.creativeKey}@${file?.name}"
        return DisplayItem(id, file, media?.isVideo == true, media?.mime)
    }

    private fun showPlaceholder() {
        runtime.playerState = if (runtime.state.identity == null) "UNPAIRED" else "IDLE"
        runtime.current = null
        val ph = DisplayItem("placeholder", null, false, null)
        if (stage.front.loadedId == ph.id) return
        stage.back.prepare(ph, stageW, stageH, onReady = {
            stage.swap(0, onSwapped = { onShowing(ManifestCodec.defaultLabel("FALLBACK"), null) }, onFadeDone = {})
        }, onPrepareError = {})
    }

    private fun footerText(c: Manifest.CreativeRef?): String =
        if (c == null) ManifestCodec.defaultLabel("FALLBACK") else "${c.label} — ${c.code}"

    private fun reportProgress() {
        val s = showing ?: return
        val now = SystemClock.elapsedRealtime()
        if (s.isVideo && !s.videoEnded) {
            val pos = stage.front.videoPositionMs() ?: return
            if (pos != s.lastVideoPos) {
                s.lastVideoPos = pos
                watchdog.lastProgressElapsed = now
            }
        } else {
            watchdog.lastProgressElapsed = now
        }
    }

    companion object {
        private const val TAG = "RdPlayer"
    }
}
