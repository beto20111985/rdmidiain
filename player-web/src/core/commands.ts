/**
 * Comandos remotos (RDPP §4/§8): `command {command_id, command, args, expires_at_ms}` → `command_ack`.
 *  - Idempotência: o servidor reentrega até receber ack FINAL; o player persiste o estado por command_id e
 *    nunca reexecuta um comando já concluído (só reenvia o ack final).
 *  - Expirado ⇒ FAILED (EXPIRED). Desconhecido ⇒ UNSUPPORTED.
 *  - restart_app/reboot: persiste "PENDING_RESTART" ANTES de reiniciar; após voltar envia DONE (`resumePending`).
 */

import type { AckStatus, CommandData } from './protocol';
import { STATE_KEYS, type StateStore } from './storage';
import { errorMessage, Mutex } from './util';

export type HandlerOutcome =
  | { status: 'DONE' | 'FAILED' | 'UNSUPPORTED'; detail?: string | null }
  /** O handler vai reiniciar o app/sistema: DONE será enviado após o retorno. */
  | { status: 'RESTARTING'; detail?: string | null; restart: () => void };

export type CommandHandler = (cmd: CommandData) => Promise<HandlerOutcome>;

interface CommandRecord {
  status: AckStatus | 'PENDING_RESTART';
  command?: string;
  detail: string | null;
  at_ms: number;
}

type CommandBook = Record<string, CommandRecord>;

export interface CommandProcessorDeps {
  store: StateStore;
  send: (type: 'command_ack', data: { command_id: string; status: AckStatus; detail: string | null }) => boolean;
  handlers: Partial<Record<string, CommandHandler>>;
  now: () => number;
  /** Quantos registros manter (os mais antigos são descartados). */
  maxRecords?: number;
}

const FINAL: ReadonlySet<string> = new Set(['DONE', 'FAILED', 'UNSUPPORTED']);

export class CommandProcessor {
  private readonly mutex = new Mutex();

  constructor(private readonly deps: CommandProcessorDeps) {}

  handle(cmd: CommandData): Promise<void> {
    return this.mutex.run(async () => {
      const book = await this.load();
      const prev = book[cmd.command_id];
      if (prev && FINAL.has(prev.status)) {
        this.ack(cmd.command_id, prev.status as AckStatus, prev.detail);
        return;
      }
      if (prev?.status === 'PENDING_RESTART') {
        // Reentregue depois do reinício: concluído.
        await this.put(book, cmd.command_id, 'DONE', prev.detail);
        this.ack(cmd.command_id, 'DONE', prev.detail);
        return;
      }
      if (cmd.expires_at_ms !== null && cmd.expires_at_ms < this.deps.now()) {
        await this.put(book, cmd.command_id, 'FAILED', 'EXPIRED');
        this.ack(cmd.command_id, 'FAILED', 'EXPIRED');
        return;
      }
      const handler = this.deps.handlers[cmd.command];
      if (!handler) {
        await this.put(book, cmd.command_id, 'UNSUPPORTED', `comando não suportado: ${cmd.command}`);
        this.ack(cmd.command_id, 'UNSUPPORTED', `comando não suportado: ${cmd.command}`);
        return;
      }
      await this.put(book, cmd.command_id, 'RECEIVED', null);
      this.ack(cmd.command_id, 'RECEIVED', null);
      let outcome: HandlerOutcome;
      try {
        outcome = await handler(cmd);
      } catch (e) {
        outcome = { status: 'FAILED', detail: errorMessage(e) };
      }
      if (outcome.status === 'RESTARTING') {
        await this.put(book, cmd.command_id, 'PENDING_RESTART', outcome.detail ?? null, cmd.command);
        outcome.restart();
        return;
      }
      await this.put(book, cmd.command_id, outcome.status, outcome.detail ?? null);
      this.ack(cmd.command_id, outcome.status, outcome.detail ?? null);
    });
  }

  /** Após (re)conectar: conclui comandos de reinício pendentes e reenvia acks finais recentes. */
  resumePending(): Promise<void> {
    return this.mutex.run(async () => {
      const book = await this.load();
      let changed = false;
      for (const [id, rec] of Object.entries(book)) {
        if (rec.status === 'PENDING_RESTART') {
          book[id] = { status: 'DONE', detail: rec.detail, at_ms: this.deps.now(), ...(rec.command ? { command: rec.command } : {}) };
          changed = true;
          this.ack(id, 'DONE', rec.detail);
        }
      }
      if (changed) await this.deps.store.set(STATE_KEYS.commands, book);
    });
  }

  /** Conclui um comando pendente (ex.: update_app que falhou em segundo plano). */
  complete(command: string, status: 'DONE' | 'FAILED', detail: string | null): Promise<boolean> {
    return this.mutex.run(async () => {
      const book = await this.load();
      const entry = Object.entries(book).find(([, r]) => r.status === 'PENDING_RESTART' && r.command === command);
      if (!entry) return false;
      await this.put(book, entry[0], status, detail, command);
      this.ack(entry[0], status, detail);
      return true;
    });
  }

  private ack(command_id: string, status: AckStatus, detail: string | null): void {
    this.deps.send('command_ack', { command_id, status, detail });
  }

  private async load(): Promise<CommandBook> {
    return (await this.deps.store.get<CommandBook>(STATE_KEYS.commands)) ?? {};
  }

  private async put(book: CommandBook, id: string, status: CommandRecord['status'], detail: string | null, command?: string): Promise<void> {
    book[id] = { status, detail, at_ms: this.deps.now(), ...(command ? { command } : {}) };
    const max = this.deps.maxRecords ?? 200;
    const entries = Object.entries(book);
    if (entries.length > max) {
      entries.sort((a, b) => a[1].at_ms - b[1].at_ms);
      for (const [k] of entries.slice(0, entries.length - max)) delete book[k];
    }
    await this.deps.store.set(STATE_KEYS.commands, book);
  }
}
