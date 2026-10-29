/**
 * Runtime de reprodução (sem DOM) — aplica as regras de RUNTIME do RDPP §5.1/§5.2/§7/§8.2 sobre `resolve()`:
 *
 *  - Agenda absoluta: cada passo começa no instante programado (tempo corrigido); nunca exibe antes.
 *  - Entrada no meio de um ITEM (boot, troca de manifesto, ressincronização, retorno de emergência, atraso grande):
 *    FILLER (fallback, sem PoP) até o fim do item + evento SKIPPED (started = finished = detecção, actual 0).
 *  - Emergência: item que seria preemptado não começa (SKIPPED + FILLER até o início da emergência); itens regulares
 *    (não-fallback) programados dentro da janela geram SKIPPED quando a janela os alcança.
 *  - Troca de manifesto SOMENTE na fronteira de ciclo (fase == 0) — ou, se uma emergência atravessar a fronteira,
 *    na primeira transição após ela (com regra de meio de item).
 *  - PoP: início visível = instante do `show()` (o crossfade conta para o novo item, §8.2.10); fim = instante em que
 *    o próximo é exibido. `actual_ms` monotônico; COMPLETED só com término integral.
 *  - Itens ESTRITOS (EXTERNAL/INSTITUTIONAL) nunca são cortados: se começaram atrasados, terminam atrasados.
 *    Itens FLEXÍVEIS (INTERNAL/FALLBACK) absorvem o atraso acumulado (> `absorbMs`) para realinhar a grade.
 *  - Vídeo: aguarda o evento de fim do decodificador por até `videoGraceMs` além do mínimo; menor que o slot ⇒
 *    segura o último quadro.
 *  - Kill/erro/recuperação do watchdog interrompem o item atual (INTERRUPTED/ERROR) e reentram na grade.
 */

import {
  fallbackKeyAt,
  isCycleBoundary,
  nextEmergencyChange,
  resolveDetailed,
  resolveRegular,
  type DetailedSegment,
  type ResolveContext,
} from './cycle';
import { decideResult, measureActualMs, type PopCreativeRef, type PopInput } from './pop';
import type { CreativeEntry, Manifest, PopResult } from './protocol';
import { errorMessage, realTimers, type MonoClock, type Timers } from './util';

export interface PlaybackContext {
  manifest: Manifest;
  version: number;
  resolve: ResolveContext;
}

export interface RenderItem {
  creativeKey: string;
  mediaKey: string;
  sha256: string;
  mime: string;
  isVideo: boolean;
  mediaDurationMs: number | null;
  url: string;
  label: string;
  code: string;
  /** Libera a URL (blob:) quando o renderer descartar a camada. */
  release: () => void;
}

export interface PreparedHandle {
  readonly item: RenderItem | null;
}

export type MediaEvent = 'ended' | 'error';

export interface Renderer {
  /** Pré-carrega no buffer de trás; rejeita se a mídia não carregar/decodificar. */
  prepare(item: RenderItem): Promise<PreparedHandle>;
  /** Tela institucional embutida (sem mídia). */
  builtin(): PreparedHandle;
  /** Exibe agora com crossfade (o novo por cima); inicia o vídeo. */
  show(h: PreparedHandle, opts: { fadeMs: number; footerText: string | null }): void;
  /** Descarta um handle preparado e não exibido. */
  discard(h: PreparedHandle): void;
  /** Eventos de mídia de um handle exibido. */
  onMediaEvent(h: PreparedHandle, cb: (ev: MediaEvent) => void): () => void;
  /** Recria as camadas (watchdog). */
  reset(): void;
  clear(): void;
}

export type EntryReason = 'BOOT' | 'SWITCH' | 'RESYNC' | 'INTERRUPT' | 'LATE';

export interface Step {
  seg: DetailedSegment | null;
  /** Criativo exibido (null ⇒ tela institucional embutida). */
  creativeKey: string | null;
  start: number;
  end: number;
  /** Item tocado desde o início ⇒ gera PoP. FILLER/idle nunca. */
  pop: boolean;
  strict: boolean;
  version: number | null;
}

export interface SkipInfo {
  creativeKey: string;
  insertionId: string | null;
  startMs: number;
  durationMs: number;
}

interface Planned {
  step: Step;
  skips: SkipInfo[];
  ctx: PlaybackContext | null;
}

interface Active {
  plan: Planned;
  handle: PreparedHandle;
  startMono: number;
  /** Instante corrigido REAL da exibição (base do agendamento). */
  showWall: number;
  /** Início do registro de PoP (= showWall, ou deslocado para não sobrepor o registro anterior). */
  startWall: number;
  /** ms descontados do fim do registro (sobreposição criada por correção do relógio para trás). */
  trimEndMs: number;
  offsetAtStart: number;
  offlineAtStart: boolean;
  ended: boolean;
  error: boolean;
  /** Fluxo interrompido (kill, erro, watchdog, ressincronização). */
  cut: boolean;
  cutReason: string | null;
  /** Qualidade comprometida (página oculta, congelamento): nunca COMPLETED. */
  degraded: boolean;
  mediaMissing: boolean;
  unsubscribe: () => void;
}

export interface EngineConfig {
  entryToleranceMs: number;
  absorbMs: number;
  videoGraceMs: number;
  maxLateMs: number;
  prepareTimeoutMs: number;
  idleRecheckMs: number;
}

export const DEFAULT_ENGINE_CONFIG: EngineConfig = {
  entryToleranceMs: 1_500,
  absorbMs: 500,
  videoGraceMs: 1_500,
  maxLateMs: 5_000,
  prepareTimeoutMs: 8_000,
  idleRecheckMs: 5_000,
};

export interface ItemStartInfo {
  creativeKey: string | null;
  code: string | null;
  startedAtMs: number;
  step: Step;
}

export interface EngineDeps {
  renderer: Renderer;
  clock: { now(): number; readonly offsetMs: number };
  mono: MonoClock;
  timers?: Timers;
  /** Contexto ativo (manifesto ativo + âncora + kills + mídia pronta), ou null. */
  getContext: () => PlaybackContext | null;
  /** Contexto da versão staged (se houver), para pré-carregar o 1º item do ciclo seguinte. */
  peekStagedContext: () => PlaybackContext | null;
  hasStaged: () => boolean;
  /** COMMIT da staged (atômico). true se trocou. */
  commitStaged: () => Promise<boolean>;
  /** Monta o item renderizável (URL da mídia), ou null se a mídia não estiver disponível. */
  mediaFor: (ctx: PlaybackContext, creativeKey: string) => Promise<RenderItem | null>;
  pop: { record(input: PopInput): Promise<unknown> };
  isOffline: () => boolean;
  config?: Partial<EngineConfig>;
  onItemStart?: (info: ItemStartInfo) => void;
  onPopResult?: (result: PopResult, step: Step) => void;
  onMediaError?: (creativeKey: string, reason: string) => void;
  log?: (msg: string) => void;
}

const BUILTIN_FOOTER = 'RD Mídia Indoor';

export class PlaybackEngine {
  private readonly cfg: EngineConfig;
  private readonly timers: Timers;
  private running = false;
  private loopPromise: Promise<void> | null = null;
  private active: Active | null = null;
  private wake: (() => void) | null = null;
  private pendingReason: EntryReason | null = null;
  private lastAnchor: number | null = null;
  private readonly skippedKeys: string[] = [];
  private readonly skippedSet = new Set<string>();

  constructor(private readonly deps: EngineDeps) {
    this.cfg = { ...DEFAULT_ENGINE_CONFIG, ...(deps.config ?? {}) };
    this.timers = deps.timers ?? realTimers;
  }

  get isRunning(): boolean {
    return this.running;
  }

  /** Passo em exibição (para heartbeat/rodapé). */
  current(): { step: Step; startedAtMs: number; code: string | null } | null {
    const a = this.active;
    if (!a) return null;
    const ctx = a.plan.ctx;
    const code = a.plan.step.creativeKey && ctx ? (ctx.manifest.creatives[a.plan.step.creativeKey]?.code ?? null) : null;
    return { step: a.plan.step, startedAtMs: a.startWall, code };
  }

  start(): void {
    if (this.running) return;
    this.running = true;
    this.loopPromise = this.loop().catch((e) => {
      this.deps.log?.(`playback: loop abortado: ${errorMessage(e)}`);
      this.running = false;
    });
  }

  async stop(): Promise<void> {
    this.running = false;
    this.poke();
    await this.loopPromise;
    this.loopPromise = null;
    if (this.active) {
      this.active.unsubscribe();
      this.active = null;
    }
    this.deps.renderer.clear();
  }

  /** Contexto mudou (kill, mídia pronta, âncora, nova ativa). */
  refresh(): void {
    const a = this.active;
    const ctx = this.deps.getContext();
    if (a && a.plan.ctx && !ctx) {
      // Contexto removido (revogação/limpeza de cache): para o conteúdo na hora.
      this.interrupt('INTERRUPT', 'NO_CONTEXT');
      return;
    }
    if (ctx && this.lastAnchor !== null && ctx.resolve.anchorMs !== this.lastAnchor) {
      this.interrupt('RESYNC');
      return;
    }
    if (a && a.plan.step.creativeKey && a.plan.ctx && ctx) {
      const c = a.plan.ctx.manifest.creatives[a.plan.step.creativeKey];
      if (c && ctx.resolve.revoked.has(c.creative_id)) {
        this.interrupt('INTERRUPT', 'KILL');
        return;
      }
    }
    if (!a || a.plan.ctx === null) this.interrupt('SWITCH');
  }

  private takeReason(): EntryReason {
    const r = this.pendingReason ?? 'INTERRUPT';
    this.pendingReason = null;
    return r;
  }

  /** Interrompe o item atual e reentra na grade agora. */
  interrupt(reason: EntryReason, detail: string = reason): void {
    if (this.active) {
      this.active.cut = true;
      this.active.cutReason = detail;
    }
    this.pendingReason = reason;
    this.poke();
  }

  /** Página oculta, quadro congelado etc.: o item atual não pode ser COMPLETED. */
  markDegraded(reason: string): void {
    if (this.active && !this.active.degraded) {
      this.active.degraded = true;
      this.deps.log?.(`playback: item degradado (${reason})`);
    }
  }

  /** Watchdog: recria as camadas e reentra. */
  recover(reason: string): void {
    this.deps.log?.(`playback: recuperação (${reason})`);
    this.markDegraded(reason);
    this.deps.renderer.reset();
    this.interrupt('INTERRUPT', reason);
  }

  // ───────────────────────── laço principal ─────────────────────────

  private async loop(): Promise<void> {
    let b = this.deps.clock.now();
    let reason: EntryReason | null = 'BOOT';
    let prevStart: number | null = null;
    let preload: { plan: Planned; handle: Promise<PreparedHandle> } | null = null;

    while (this.running) {
      // 1) contexto (COMMIT na fronteira de ciclo)
      const prevCtx = this.active?.plan.ctx ?? null;
      const { ctx, switched } = await this.contextAt(b, prevStart, true);
      if (!this.running) break;
      if (switched) reason ??= 'SWITCH';
      if (ctx && prevCtx === null && this.active) reason ??= 'SWITCH';
      if (ctx && this.lastAnchor !== null && ctx.resolve.anchorMs !== this.lastAnchor) reason ??= 'RESYNC';
      if (ctx) this.lastAnchor = ctx.resolve.anchorMs;

      // 2) plano + 3) mídia preparada (reaproveita o pré-carregado se o plano é o mesmo)
      const plan = this.plan(ctx, b, reason);
      let handle: PreparedHandle;
      let mediaMissing = false;
      if (preload && samePlan(preload.plan, plan)) {
        const r = await this.awaitPrepared(preload.handle);
        handle = r.handle;
        mediaMissing = r.missing;
      } else {
        if (preload) void preload.handle.then((h) => this.deps.renderer.discard(h)).catch(() => undefined);
        const fresh = this.prepare(plan);
        fresh.catch(() => undefined);
        const r = await this.awaitPrepared(fresh);
        handle = r.handle;
        mediaMissing = r.missing;
      }
      preload = null;
      if (!this.running) {
        this.deps.renderer.discard(handle);
        break;
      }

      // 4) nunca exibe antes do instante programado
      await this.waitWall(plan.step.start, () => this.pendingReason !== null);
      if (!this.running) {
        this.deps.renderer.discard(handle);
        break;
      }
      if (this.pendingReason !== null) {
        // Interrompido antes de exibir (kill/ressincronização): replaneja agora.
        this.deps.renderer.discard(handle);
        reason = this.takeReason();
        b = this.deps.clock.now();
        continue;
      }

      // 5) exibe e fecha a medição do anterior no MESMO instante (intervalos contíguos, sem sobreposição)
      const showMono = this.deps.mono();
      const showWall = this.deps.clock.now();
      const fadeMs = plan.ctx?.manifest.settings.fade_ms ?? 400;
      this.deps.renderer.show(handle, { fadeMs, footerText: this.footerText(plan, handle) });
      const previous = this.active;
      const startWall = previous ? this.chainStart(previous, showMono, showWall) : showWall;
      const active: Active = {
        plan,
        handle,
        startMono: showMono,
        showWall,
        startWall,
        trimEndMs: 0,
        offsetAtStart: this.deps.clock.offsetMs,
        offlineAtStart: this.deps.isOffline(),
        ended: false,
        error: false,
        cut: false,
        cutReason: null,
        degraded: false,
        mediaMissing,
        unsubscribe: () => undefined,
      };
      active.unsubscribe = this.deps.renderer.onMediaEvent(handle, (ev) => {
        if (ev === 'ended') active.ended = true;
        if (ev === 'error') {
          active.error = true;
          if (this.active === active) this.interrupt('INTERRUPT', 'MEDIA_ERROR');
        }
        this.poke();
      });
      this.active = active;
      if (previous) {
        previous.unsubscribe();
        await this.finish(previous, showMono);
      }
      await this.emitSkips(plan);
      if (plan.step.pop && mediaMissing) await this.recordMissing(active);
      this.deps.onItemStart?.({
        creativeKey: plan.step.creativeKey,
        code: this.creative(plan, plan.step.creativeKey)?.code ?? null,
        startedAtMs: active.startWall,
        step: plan.step,
      });

      // 6) pré-carrega o próximo (double buffer)
      const nextB = plan.step.end;
      if (plan.ctx) {
        const peek = await this.contextAt(nextB, plan.step.start, false);
        const nextPlan = this.plan(peek.ctx, nextB, null);
        const nextHandle = this.prepare(nextPlan);
        nextHandle.catch(() => undefined); // rejeição tratada quando (e se) for aguardado
        preload = { plan: nextPlan, handle: nextHandle };
      }

      // 7) aguarda o fim do passo
      const outcome = await this.waitEnd(active);
      if (!this.running) break;
      prevStart = plan.step.start;
      if (outcome === 'INTERRUPT') {
        reason = this.takeReason();
        b = this.deps.clock.now();
        if (preload) {
          const p = preload;
          void p.handle.then((h) => this.deps.renderer.discard(h)).catch(() => undefined);
          preload = null;
        }
        continue;
      }
      if (plan.ctx === null) {
        // idle: reentra no instante atual
        reason = 'SWITCH';
        b = this.deps.clock.now();
        continue;
      }
      reason = null;
      b = plan.step.end;
      if (this.deps.clock.now() - b > this.cfg.maxLateMs) {
        reason = 'LATE';
        b = this.deps.clock.now();
      }
    }
    if (preload) void preload.handle.then((h) => this.deps.renderer.discard(h)).catch(() => undefined);
    // Parada: o item em exibição não é registrado (app parando/revogado).
  }

  /** Contexto para o passo que começa em `b`; com `commit`, faz a troca de manifesto na fronteira de ciclo. */
  private async contextAt(
    b: number,
    prevStart: number | null,
    commit: boolean,
  ): Promise<{ ctx: PlaybackContext | null; switched: boolean }> {
    const cur = this.deps.getContext();
    if (!cur || !this.deps.hasStaged()) return { ctx: cur, switched: false };
    const A = cur.resolve.anchorMs;
    const C = cur.manifest.rotation.cycle_ms;
    const atBoundary = isCycleBoundary(cur.manifest, A, b);
    const crossed = !atBoundary && prevStart !== null && Math.floor((b - A) / C) > Math.floor((prevStart - A) / C);
    if (!atBoundary && !crossed) return { ctx: cur, switched: false };
    if (!commit) return { ctx: this.deps.peekStagedContext() ?? cur, switched: false };
    const ok = await this.deps.commitStaged().catch((e) => {
      this.deps.log?.(`playback: commit falhou: ${errorMessage(e)}`);
      return false;
    });
    const next = this.deps.getContext();
    // Troca exatamente na fronteira não é "entrada"; atravessada no meio (emergência) aplica a regra de meio de item.
    return { ctx: next, switched: ok && crossed };
  }

  /** Planejamento PURO do passo em `b` (efeitos colaterais — SKIPPED — só quando o passo é efetivado). */
  private plan(ctx: PlaybackContext | null, b: number, reason: EntryReason | null): Planned {
    if (!ctx) {
      return {
        step: { seg: null, creativeKey: null, start: b, end: b + this.cfg.idleRecheckMs, pop: false, strict: false, version: null },
        skips: [],
        ctx: null,
      };
    }
    const seg = resolveDetailed(ctx.resolve, b);
    const cut = nextEmergencyChange(ctx.manifest, b, seg.end_ms);
    const end = cut ?? seg.end_ms;
    const skips: SkipInfo[] = [];
    if (seg.block === 'EMERGENCY') skips.push(...this.preemptedBy(ctx, b, end));
    const filler = (key: string | null, until: number): Step => ({
      seg,
      creativeKey: key,
      start: b,
      end: until,
      pop: false,
      strict: false,
      version: ctx.version,
    });
    if (seg.kind === 'FILLER') return { step: filler(seg.creative_key, end), skips, ctx };
    const tol = reason === 'BOOT' || reason === 'SWITCH' || reason === 'RESYNC' ? this.cfg.entryToleranceMs : 0;
    if (b - seg.start_ms > tol) {
      // Entrada no meio do item: FILLER até o fim do item + SKIPPED (não em interrupção por kill/erro).
      // Retorno de emergência (reason null) sobre fallback: fallback preemptado não gera SKIPPED (§5.2).
      if (reason !== 'INTERRUPT' && seg.creative_key && !(reason === null && seg.source === 'FALLBACK')) skips.push(skipOf(seg));
      return { step: filler(fallbackKeyAt(ctx.resolve, b), end), skips, ctx };
    }
    if (cut !== null) {
      // Seria preemptado por emergência: não começa.
      if (seg.source !== 'FALLBACK' && seg.creative_key) skips.push(skipOf(seg));
      return { step: filler(fallbackKeyAt(ctx.resolve, b), cut), skips, ctx };
    }
    const kind = seg.creative_key ? ctx.manifest.creatives[seg.creative_key]?.kind : undefined;
    return {
      step: {
        seg,
        creativeKey: seg.creative_key,
        start: seg.start_ms,
        end: seg.end_ms,
        pop: true,
        strict: seg.block !== 'EMERGENCY' && (kind === 'EXTERNAL' || kind === 'INSTITUTIONAL'),
        version: ctx.version,
      },
      skips,
      ctx,
    };
  }

  /** Itens regulares (não-fallback) que começam em [from, to) durante uma emergência. */
  private preemptedBy(ctx: PlaybackContext, from: number, to: number): SkipInfo[] {
    const out: SkipInfo[] = [];
    let t = from;
    let guard = 0;
    while (t < to && guard++ < 10_000) {
      const s = resolveRegular(ctx.resolve, t);
      if (s.kind === 'ITEM' && s.source !== 'FALLBACK' && s.start_ms >= from && s.creative_key) out.push(skipOf(s));
      t = s.end_ms;
    }
    return out;
  }

  private async emitSkips(plan: Planned): Promise<void> {
    const ctx = plan.ctx;
    if (!ctx) return;
    for (const s of plan.skips) {
      const key = `${ctx.version}:${s.creativeKey}@${s.startMs}`;
      if (this.skippedSet.has(key)) continue;
      this.skippedSet.add(key);
      this.skippedKeys.push(key);
      if (this.skippedKeys.length > 1_000) this.skippedSet.delete(this.skippedKeys.shift()!);
      const c = ctx.manifest.creatives[s.creativeKey];
      if (!c) continue;
      try {
        await this.deps.pop.record({
          creative: creativeRef(c),
          insertion_id: s.insertionId,
          manifest_version: ctx.version,
          started_at_ms: this.deps.clock.now(),
          expected_ms: s.durationMs,
          actual_ms: 0,
          result: 'SKIPPED',
          clock_offset_ms: this.deps.clock.offsetMs,
          offline: this.deps.isOffline(),
        });
      } catch (e) {
        this.deps.log?.(`pop: falha ao gravar SKIPPED: ${errorMessage(e)}`);
      }
    }
  }

  private async recordMissing(active: Active): Promise<void> {
    const plan = active.plan;
    const c = this.creative(plan, plan.step.creativeKey);
    if (!c || !plan.ctx) return;
    try {
      await this.deps.pop.record({
        creative: creativeRef(c),
        insertion_id: plan.step.seg?.insertion_id ?? null,
        manifest_version: plan.ctx.version,
        started_at_ms: active.startWall,
        expected_ms: plan.step.end - plan.step.start,
        actual_ms: 0,
        result: 'ERROR',
        clock_offset_ms: active.offsetAtStart,
        offline: active.offlineAtStart,
      });
    } catch (e) {
      this.deps.log?.(`pop: falha ao gravar ERROR: ${errorMessage(e)}`);
    }
    this.deps.onPopResult?.('ERROR', plan.step);
  }

  /**
   * Início do registro de PoP do novo passo. Regra (achado do E2E real): cada passo é ancorado no relógio corrigido
   * ATUAL (nunca herda a base de passos anteriores — uma correção do offset não pode deslocar a cadeia para sempre).
   * Só entre dois registros de PoP consecutivos garante-se a não sobreposição: se o relógio voltou (d > 0), primeiro
   * desconta-se do fim do anterior o que ele durou além do esperado (a espera extra que a própria volta causou); o
   * resto (raro) desloca o início do novo, e a primeira folga seguinte ressincroniza.
   */
  private chainStart(previous: Active, showMono: number, showWall: number): number {
    const chained = previous.plan.step.pop && !previous.mediaMissing && previous.plan.ctx !== null;
    if (!chained) return showWall;
    const measured = measureActualMs(previous.startMono, showMono);
    const d = previous.startWall + measured - showWall;
    if (d <= 0) return showWall;
    const expectedPrev = previous.plan.step.end - previous.plan.step.start;
    const trim = Math.min(d, Math.max(0, measured - expectedPrev));
    previous.trimEndMs = trim;
    return showWall + (d - trim);
  }

  /** Fecha a medição do passo anterior e grava o PoP. */
  private async finish(a: Active, endMono: number): Promise<void> {
    const plan = a.plan;
    if (!plan.step.pop || a.mediaMissing || !plan.ctx) return;
    const c = this.creative(plan, plan.step.creativeKey);
    if (!c) return;
    const expected = plan.step.end - plan.step.start;
    const actual = Math.max(0, measureActualMs(a.startMono, endMono) - a.trimEndMs);
    const result = decideResult({
      actualMs: actual,
      expectedMs: expected,
      isVideo: a.handle.item?.isVideo ?? false,
      videoEnded: a.ended,
      error: a.error,
      interrupted: a.cut || a.degraded,
    });
    try {
      await this.deps.pop.record({
        creative: creativeRef(c),
        insertion_id: plan.step.seg?.insertion_id ?? null,
        manifest_version: plan.ctx.version,
        started_at_ms: a.startWall,
        expected_ms: expected,
        actual_ms: actual,
        result,
        clock_offset_ms: a.offsetAtStart,
        offline: a.offlineAtStart,
      });
    } catch (e) {
      this.deps.log?.(`pop: falha ao gravar evento: ${errorMessage(e)}`);
    }
    this.deps.onPopResult?.(result, plan.step);
  }

  /** Espera o fim do passo conforme a regra de estrito/flexível. */
  private async waitEnd(a: Active): Promise<'NORMAL' | 'INTERRUPT'> {
    const step = a.plan.step;
    const interrupted = (): boolean => this.pendingReason !== null || !this.running;
    if (!step.pop || a.mediaMissing) {
      await this.waitWall(step.end, interrupted);
      return this.pendingReason !== null ? 'INTERRUPT' : 'NORMAL';
    }
    const expected = step.end - step.start;
    const schedEndMono = a.startMono + (step.end - a.showWall);
    const minEndMono = a.startMono + expected;
    const lateness = minEndMono - schedEndMono;
    const target = step.strict
      ? Math.max(schedEndMono, minEndMono)
      : lateness > this.cfg.absorbMs
        ? schedEndMono
        : Math.max(schedEndMono, minEndMono);
    await this.waitMono(target, interrupted);
    if (interrupted()) return this.pendingReason !== null ? 'INTERRUPT' : 'NORMAL';
    if (a.handle.item?.isVideo && !a.ended && !a.error && target >= minEndMono) {
      // Aguarda o fim do decodificador (vídeo até +40 ms do slot, ou buffering breve).
      const graceEnd = this.deps.mono() + this.cfg.videoGraceMs;
      await this.waitMono(graceEnd, () => interrupted() || a.ended || a.error);
    }
    return this.pendingReason !== null ? 'INTERRUPT' : 'NORMAL';
  }

  private async prepare(plan: Planned): Promise<PreparedHandle> {
    const key = plan.step.creativeKey;
    if (!key || !plan.ctx) return this.deps.renderer.builtin();
    const item = await this.deps.mediaFor(plan.ctx, key).catch(() => null);
    if (!item) {
      if (plan.step.pop) this.deps.onMediaError?.(key, 'MEDIA_UNAVAILABLE');
      throw new MissingMedia();
    }
    try {
      return await this.deps.renderer.prepare(item);
    } catch (e) {
      item.release();
      this.deps.onMediaError?.(key, `DECODE: ${errorMessage(e)}`);
      throw new MissingMedia();
    }
  }

  private async awaitPrepared(p: Promise<PreparedHandle>): Promise<{ handle: PreparedHandle; missing: boolean }> {
    let timer: unknown = null;
    const timeout = new Promise<'timeout'>((res) => {
      timer = this.timers.setTimeout(() => res('timeout'), this.cfg.prepareTimeoutMs);
    });
    try {
      const r = await Promise.race([p, timeout]);
      if (r === 'timeout') {
        void p.then((h) => this.deps.renderer.discard(h)).catch(() => undefined);
        return { handle: this.deps.renderer.builtin(), missing: true };
      }
      return { handle: r, missing: false };
    } catch {
      return { handle: this.deps.renderer.builtin(), missing: true };
    } finally {
      if (timer !== null) this.timers.clearTimeout(timer);
    }
  }

  private footerText(plan: Planned, handle: PreparedHandle): string | null {
    if (plan.ctx && !plan.ctx.manifest.settings.footer.enabled) return null;
    if (!handle.item) return BUILTIN_FOOTER;
    return `${handle.item.label} — ${handle.item.code}`;
  }

  private creative(plan: Planned, key: string | null): CreativeEntry | undefined {
    return key && plan.ctx ? plan.ctx.manifest.creatives[key] : undefined;
  }

  // ───────────────────────── esperas interrompíveis ─────────────────────────

  private poke(): void {
    const w = this.wake;
    this.wake = null;
    w?.();
  }

  private sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
      let done = false;
      const finish = (): void => {
        if (done) return;
        done = true;
        this.timers.clearTimeout(h);
        if (this.wake === finish) this.wake = null;
        resolve();
      };
      const h = this.timers.setTimeout(finish, Math.max(0, ms));
      this.wake = finish;
    });
  }

  private async waitMono(target: number, stop: () => boolean): Promise<void> {
    for (;;) {
      if (stop()) return;
      const remaining = target - this.deps.mono();
      if (remaining <= 0) return;
      await this.sleep(Math.ceil(remaining));
    }
  }

  private async waitWall(target: number, stop: () => boolean): Promise<void> {
    for (;;) {
      if (stop() || !this.running) return;
      const remaining = target - this.deps.clock.now();
      if (remaining <= 0) return;
      await this.sleep(Math.ceil(remaining));
    }
  }
}

class MissingMedia extends Error {
  constructor() {
    super('mídia indisponível');
    this.name = 'MissingMedia';
  }
}

function skipOf(s: DetailedSegment): SkipInfo {
  return { creativeKey: s.creative_key!, insertionId: s.insertion_id, startMs: s.start_ms, durationMs: s.end_ms - s.start_ms };
}

export function creativeRef(c: CreativeEntry): PopCreativeRef {
  return { creative_id: c.creative_id, code: c.code, campaign_id: c.campaign_id, kind: c.kind };
}

function samePlan(a: Planned, b: Planned): boolean {
  return (
    a.ctx?.version === b.ctx?.version &&
    a.step.creativeKey === b.step.creativeKey &&
    a.step.start === b.step.start &&
    a.step.end === b.step.end &&
    a.step.pop === b.step.pop &&
    a.ctx?.resolve.mediaReady.size === b.ctx?.resolve.mediaReady.size &&
    a.ctx?.resolve.revoked.size === b.ctx?.resolve.revoked.size
  );
}
