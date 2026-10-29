/**
 * Relógio corrigido do player (RDPP §5.1 "t = relógio local + clock_offset_ms", §5.1.10 alerta de relógio).
 *
 * - O offset é medido pelo `X-Server-Time-Ms` de toda resposta /api/v1/player/*, pelo `server_time_ms` do token
 *   e pela mensagem WS `server_time`. Com RTT conhecido, usa o ponto médio; mantém a amostra de menor RTT
 *   entre as últimas N (robusto a picos de latência).
 * - Persistência: offset + "marca d'água" (maior instante corrigido já visto) — detecta RTC zerado após queda
 *   de energia quando não há rede para corrigir.
 * - Alerta "Corrija a data e hora" quando: |offset| > 120 s sem medição nesta sessão; instante corrigido anterior
 *   ao `generated_at_ms` do manifesto ativo; ou relógio voltou > 10 min em relação à marca d'água.
 */

import { realMono, type MonoClock } from './util';

export interface ClockRecord {
  offset_ms: number;
  measured_at_ms: number | null;
  hwm_ms: number;
}

export type ClockIssue = 'OFFSET_UNCORRECTED' | 'BEFORE_GENERATED' | 'WENT_BACKWARDS';

export interface ClockStatus {
  valid: boolean;
  issues: ClockIssue[];
}

interface Sample {
  offset: number;
  rtt: number;
  atMono: number;
}

export interface ClockOptions {
  wallNow?: () => number;
  mono?: MonoClock;
  /** Amostras com RTT acima disto são descartadas. */
  maxRttMs?: number;
  /** Limite do RDPP §5.1.10. */
  invalidOffsetMs?: number;
  /** Janela de amostras para escolher a de menor RTT. */
  sampleWindow?: number;
  /** Validade de uma amostra para a seleção por menor RTT. */
  sampleTtlMs?: number;
}

export class Clock {
  private readonly wallNow: () => number;
  private readonly mono: MonoClock;
  private readonly maxRtt: number;
  private readonly invalidOffset: number;
  private readonly windowSize: number;
  private readonly sampleTtl: number;
  private samples: Sample[] = [];
  private offset = 0;
  private measuredAt: number | null = null;
  private hwm = 0;
  private measuredThisSession = false;
  private listeners = new Set<(offset: number) => void>();

  constructor(opts: ClockOptions = {}) {
    this.wallNow = opts.wallNow ?? (() => Date.now());
    this.mono = opts.mono ?? realMono;
    this.maxRtt = opts.maxRttMs ?? 5_000;
    this.invalidOffset = opts.invalidOffsetMs ?? 120_000;
    this.windowSize = opts.sampleWindow ?? 5;
    this.sampleTtl = opts.sampleTtlMs ?? 15 * 60_000;
  }

  /** Restaura o estado persistido (antes de qualquer medição). */
  load(rec: ClockRecord | undefined | null): void {
    if (!rec) return;
    if (Number.isFinite(rec.offset_ms)) this.offset = Math.round(rec.offset_ms);
    this.measuredAt = rec.measured_at_ms ?? null;
    if (Number.isFinite(rec.hwm_ms)) this.hwm = rec.hwm_ms;
  }

  snapshot(): ClockRecord {
    this.hwm = Math.max(this.hwm, this.now());
    return { offset_ms: this.offset, measured_at_ms: this.measuredAt, hwm_ms: this.hwm };
  }

  get offsetMs(): number {
    return this.offset;
  }

  get synced(): boolean {
    return this.measuredThisSession;
  }

  /** Instante corrigido (ms inteiros UTC). */
  now(): number {
    return Math.round(this.wallNow() + this.offset);
  }

  onChange(fn: (offset: number) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /**
   * Amostra do servidor. `sendMono`/`recvMono` delimitam a requisição; sem eles, assume RTT 0 (mensagem WS).
   * Retorna true se a amostra foi aceita.
   */
  observe(serverMs: number, sendMono?: number, recvMono?: number): boolean {
    if (!Number.isFinite(serverMs) || serverMs <= 0) return false;
    const nowMono = this.mono();
    const recv = recvMono ?? nowMono;
    const rtt = sendMono !== undefined ? Math.max(0, recv - sendMono) : 0;
    if (rtt > this.maxRtt) return false;
    // Instante local (parede) correspondente ao meio da requisição.
    const wallAtRecv = this.wallNow() - (nowMono - recv);
    const offset = Math.round(serverMs - (wallAtRecv - rtt / 2));
    this.samples.push({ offset, rtt, atMono: nowMono });
    this.samples = this.samples.filter((s) => nowMono - s.atMono <= this.sampleTtl).slice(-this.windowSize);
    const best = this.samples.reduce((a, b) => (b.rtt < a.rtt ? b : a));
    const changed = best.offset !== this.offset;
    this.offset = best.offset;
    this.measuredThisSession = true;
    this.measuredAt = serverMs;
    this.hwm = Math.max(this.hwm, serverMs);
    if (changed) for (const fn of this.listeners) fn(this.offset);
    return true;
  }

  /** Situação do relógio para o alerta do rodapé. */
  status(generatedAtMs: number | null = null): ClockStatus {
    const issues: ClockIssue[] = [];
    const now = this.now();
    if (!this.measuredThisSession && Math.abs(this.offset) > this.invalidOffset) issues.push('OFFSET_UNCORRECTED');
    if (generatedAtMs !== null && now < generatedAtMs) issues.push('BEFORE_GENERATED');
    if (!this.measuredThisSession && this.hwm > 0 && now < this.hwm - 10 * 60_000) issues.push('WENT_BACKWARDS');
    return { valid: issues.length === 0, issues };
  }
}
