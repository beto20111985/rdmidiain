/**
 * Prova de exibição (PoP) — RDPP §7 e §8.2.9/§8.2.10.
 *
 *  - `actual_ms` medido por relógio MONOTÔNICO entre o início visível e o fim (floor, inteiro).
 *  - `COMPLETED` somente se terminou integralmente: sem erro, sem interrupção (corte, página oculta, congelamento),
 *    vídeo com evento de fim do decodificador e `actual_ms ≥ expected_ms` (sem tolerância: 29 999/30 000 não é).
 *  - `started_at_ms`/`finished_at_ms` em tempo CORRIGIDO; `finished_at_ms = started_at_ms + actual_ms`.
 *  - `seq` persistido por dispositivo (nunca reinicia em reload/reboot), `nonce` de 16 bytes, `prev_hash` = sha256 do
 *    RDJ1 do evento anterior (ou 64 zeros). Evento + novo estado da cadeia gravados na MESMA transação.
 *  - Outbox: envio em lotes ≤ 200 (ordem de gravação); remove apenas eventos com status definitivo
 *    (ACCEPTED/DUPLICATE/REJECTED). Chave = contador global `oid` (seq reinicia só com novo device_id).
 *  - SKIPPED: started = finished = instante corrigido da detecção, actual_ms = 0, expected_ms = d do item.
 */

import { canonicalSha256Hex, signCanonical, type Signer } from './crypto';
import {
  MAX_EVENTS_PER_BATCH,
  ZERO_HASH,
  type BatchResponse,
  type CreativeKind,
  type PopEvent,
  type PopResult,
  type SignedPopEvent,
} from './protocol';
import { STATE_KEYS, type OutboxRecord, type StateStore } from './storage';
import { b64uEncode, cryptoRandomBytes, Mutex, uuidv4, type RandomBytes } from './util';

export interface PopChainState {
  device_id: string;
  seq: number;
  prev_hash: string;
  last_finished_ms: number;
}

export interface PopIdentity {
  device_id: string;
  screen_id: string;
}

export interface PopCreativeRef {
  creative_id: string;
  code: string;
  campaign_id: string | null;
  kind: CreativeKind;
}

export interface PopInput {
  creative: PopCreativeRef;
  insertion_id: string | null;
  manifest_version: number;
  started_at_ms: number;
  expected_ms: number;
  actual_ms: number;
  result: PopResult;
  clock_offset_ms: number;
  offline: boolean;
}

export interface CompletionFacts {
  actualMs: number;
  expectedMs: number;
  isVideo: boolean;
  /** Vídeo: o decodificador emitiu o evento de fim. */
  videoEnded: boolean;
  /** Falha de mídia/decodificação. */
  error: boolean;
  /** Corte, página oculta, congelamento, kill, revogação… */
  interrupted: boolean;
}

/** Duração medida (inteira, por baixo — nunca arredonda para cima). */
export function measureActualMs(startMono: number, endMono: number): number {
  return Math.max(0, Math.floor(endMono - startMono));
}

/** Regra de "100%" (A4 Q7): sem tolerância. */
export function decideResult(f: CompletionFacts): PopResult {
  if (f.error) return 'ERROR';
  if (f.interrupted) return 'INTERRUPTED';
  if (f.isVideo && !f.videoEnded) return 'INTERRUPTED';
  return f.actualMs >= f.expectedMs ? 'COMPLETED' : 'INTERRUPTED';
}

export interface FlushResult {
  sent: number;
  removed: number;
  remaining: number;
  discardedForeign: number;
}

export type BatchSender = (events: SignedPopEvent[]) => Promise<BatchResponse>;

const FINAL_STATUSES = new Set(['ACCEPTED', 'DUPLICATE', 'REJECTED']);
/** Sobreposição pequena com o evento anterior (ajuste de offset) é descontada do item; maior fica como está. */
const MAX_OVERLAP_CLAMP_MS = 5_000;

export interface PopRecorderDeps {
  store: StateStore;
  signer: () => Signer | null;
  identity: () => PopIdentity | null;
  random?: RandomBytes;
  uuid?: () => string;
  onRecorded?: (e: SignedPopEvent) => void;
}

export class PopRecorder {
  private readonly mutex = new Mutex();
  private readonly flushMutex = new Mutex();
  private readonly random: RandomBytes;
  private readonly uuid: () => string;

  constructor(private readonly deps: PopRecorderDeps) {
    this.random = deps.random ?? cryptoRandomBytes;
    this.uuid = deps.uuid ?? (() => uuidv4(this.random));
  }

  /** Grava (assinado) no outbox. Retorna null se o dispositivo não está pareado. */
  record(input: PopInput): Promise<SignedPopEvent | null> {
    return this.mutex.run(async () => {
      const id = this.deps.identity();
      const signer = this.deps.signer();
      if (!id || !signer) return null;
      const stored = await this.deps.store.get<PopChainState>(STATE_KEYS.popChain);
      const oid = ((await this.deps.store.get<number>(STATE_KEYS.popOid)) ?? 0) + 1;
      const chain: PopChainState =
        stored && stored.device_id === id.device_id
          ? stored
          : { device_id: id.device_id, seq: 0, prev_hash: ZERO_HASH, last_finished_ms: 0 };

      let started = Math.trunc(input.started_at_ms);
      let actual = input.result === 'SKIPPED' ? 0 : Math.max(0, Math.trunc(input.actual_ms));
      let result = input.result;
      const overlap = chain.last_finished_ms - started;
      if (result !== 'SKIPPED' && overlap > 0 && overlap <= MAX_OVERLAP_CLAMP_MS) {
        // Salvaguarda: começa no fim do anterior e desconta a sobreposição do próprio item (o fim não se move,
        // então o erro não se propaga aos seguintes). Sem tempo completo, não é COMPLETED.
        started = chain.last_finished_ms;
        actual = Math.max(0, actual - overlap);
        if (result === 'COMPLETED' && actual < input.expected_ms) result = 'INTERRUPTED';
      }

      const event: PopEvent = {
        schema: 'rd.pop/1',
        playback_uuid: this.uuid(),
        device_id: id.device_id,
        screen_id: id.screen_id,
        creative_id: input.creative.creative_id,
        creative_code: input.creative.code,
        campaign_id: input.creative.campaign_id,
        insertion_id: input.insertion_id,
        kind: input.creative.kind,
        manifest_version: input.manifest_version,
        started_at_ms: started,
        finished_at_ms: started + actual,
        expected_ms: Math.trunc(input.expected_ms),
        actual_ms: actual,
        result,
        seq: chain.seq + 1,
        nonce: b64uEncode(this.random(16)),
        prev_hash: chain.prev_hash,
        clock_offset_ms: Math.trunc(input.clock_offset_ms),
        offline: input.offline,
      };
      const signature = await signCanonical(signer, event);
      const next: PopChainState = {
        device_id: id.device_id,
        seq: event.seq,
        prev_hash: canonicalSha256Hex(event),
        last_finished_ms: Math.max(chain.last_finished_ms, event.finished_at_ms),
      };
      await this.deps.store.transact({
        set: { [STATE_KEYS.popChain]: next, [STATE_KEYS.popOid]: oid },
        outboxPut: [{ oid, seq: event.seq, playback_uuid: event.playback_uuid, event, signature }],
      });
      const signed = { event, signature };
      this.deps.onRecorded?.(signed);
      return signed;
    });
  }

  /** Evento SKIPPED (item perdido por entrada no meio, preempção por emergência). */
  skipped(input: Omit<PopInput, 'actual_ms' | 'result'>): Promise<SignedPopEvent | null> {
    return this.record({ ...input, actual_ms: 0, result: 'SKIPPED' });
  }

  async pendingCount(): Promise<number> {
    return this.deps.store.outboxCount();
  }

  /**
   * Envia o outbox em lotes ≤ 200, em ordem de seq. Erros de rede propagam (o chamador faz backoff).
   * Lote parcialmente confirmado: remove só os confirmados e para (o resto vai no próximo flush).
   */
  flush(send: BatchSender): Promise<FlushResult> {
    return this.flushMutex.run(async () => {
      const result: FlushResult = { sent: 0, removed: 0, remaining: 0, discardedForeign: 0 };
      const id = this.deps.identity();
      for (;;) {
        const batch = await this.deps.store.outboxList(MAX_EVENTS_PER_BATCH);
        if (batch.length === 0) break;
        // Eventos de um dispositivo anterior (re-pareamento) não podem mais ser entregues por esta identidade.
        const foreign = id ? batch.filter((r) => r.event.device_id !== id.device_id) : [];
        if (foreign.length) {
          await this.deps.store.transact({ outboxDelete: foreign.map((r) => r.oid) });
          result.discardedForeign += foreign.length;
          continue;
        }
        const resp = await send(batch.map((r: OutboxRecord) => ({ event: r.event, signature: r.signature })));
        result.sent += batch.length;
        const status = new Map<string, string>();
        for (const r of resp.results ?? []) status.set(r.playback_uuid, r.status);
        const done = batch.filter((r) => FINAL_STATUSES.has(status.get(r.playback_uuid) ?? ''));
        if (done.length) await this.deps.store.transact({ outboxDelete: done.map((r) => r.oid) });
        result.removed += done.length;
        if (done.length < batch.length) break;
      }
      result.remaining = await this.deps.store.outboxCount();
      return result;
    });
  }
}
