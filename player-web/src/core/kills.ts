/**
 * Kill (remoção emergencial — RDPP §4/§8, ETAPA2 #1237–#1239): aplicado NA HORA e persistido.
 * Permanece válido mesmo após reboot offline; o próximo manifesto também traz o criativo em `revoked`.
 *
 * Reconciliação (achado do E2E real): o kill local só precisa durar até chegar um manifesto GERADO DEPOIS dele — a
 * partir daí o `revoked` do manifesto é a fonte de verdade. Sem isso, um kill suspenso ("lift") no painel nunca
 * voltaria a tocar neste aparelho. Margem de 2 s contra diferença de relógio.
 */

import { STATE_KEYS, type StateStore } from './storage';

interface KillRecord {
  creative_ids: string[];
  /** instante (corrigido) em que cada kill foi recebido */
  at?: Record<string, number>;
  updated_at_ms: number;
}

export const KILL_RECONCILE_MARGIN_MS = 2_000;

export class KillList {
  private set = new Set<string>();
  private at = new Map<string, number>();
  private listeners = new Set<(ids: string[]) => void>();

  constructor(
    private readonly store: StateStore,
    private readonly now: () => number = () => Date.now(),
  ) {}

  async load(): Promise<void> {
    const rec = await this.store.get<KillRecord>(STATE_KEYS.kills);
    this.set = new Set(rec?.creative_ids ?? []);
    this.at = new Map([...this.set].map((id) => [id, rec?.at?.[id] ?? rec?.updated_at_ms ?? 0]));
  }

  ids(): string[] {
    return [...this.set];
  }

  has(creativeId: string): boolean {
    return this.set.has(creativeId);
  }

  onChange(fn: (ids: string[]) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** Persiste ANTES de notificar (um reboot logo depois não "ressuscita" o criativo). */
  async add(ids: readonly string[]): Promise<string[]> {
    const added = ids.filter((id) => typeof id === 'string' && id && !this.set.has(id));
    if (added.length === 0) return [];
    const now = this.now();
    const next = new Set([...this.set, ...added]);
    const at = new Map(this.at);
    for (const id of added) at.set(id, now);
    await this.persist(next, at);
    for (const fn of this.listeners) fn(added);
    return added;
  }

  /**
   * Manifesto ativado: remove kills locais anteriores à geração do manifesto e ausentes do seu `revoked`.
   * Devolve os creative_id liberados.
   */
  async reconcile(revoked: readonly string[], generatedAtMs: number): Promise<string[]> {
    const keep = new Set(revoked);
    const lifted = [...this.set].filter((id) => !keep.has(id) && generatedAtMs > (this.at.get(id) ?? 0) + KILL_RECONCILE_MARGIN_MS);
    if (lifted.length === 0) return [];
    const next = new Set([...this.set].filter((id) => !lifted.includes(id)));
    const at = new Map([...this.at].filter(([id]) => next.has(id)));
    await this.persist(next, at);
    for (const fn of this.listeners) fn(lifted);
    return lifted;
  }

  async clear(): Promise<void> {
    this.set.clear();
    this.at.clear();
    await this.store.delete(STATE_KEYS.kills);
  }

  private async persist(next: Set<string>, at: Map<string, number>): Promise<void> {
    const rec: KillRecord = { creative_ids: [...next], at: Object.fromEntries(at), updated_at_ms: this.now() };
    await this.store.set(STATE_KEYS.kills, rec);
    this.set = next;
    this.at = at;
  }
}
