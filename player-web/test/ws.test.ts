import { afterEach, describe, expect, it, vi } from 'vitest';
import { CommandProcessor } from '../src/core/commands';
import { KillList } from '../src/core/kills';
import { MemoryStateStore } from '../src/core/memoryStores';
import type { CommandData, HeartbeatData, TelemetryData } from '../src/core/protocol';
import { makeEnvelope, parseEnvelope, PlayerChannel, ReconnectingSocket, type ChannelHandlers, type WebSocketLike } from '../src/core/ws';

class FakeWs implements WebSocketLike {
  static all: FakeWs[] = [];
  readyState = 0;
  sent: string[] = [];
  onopen: ((ev: unknown) => void) | null = null;
  onclose: ((ev: { code: number; reason: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  onmessage: ((ev: { data: unknown }) => void) | null = null;
  constructor(public url: string) {
    FakeWs.all.push(this);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close(code = 1000, reason = '') {
    this.readyState = 3;
    this.onclose?.({ code, reason });
  }
  open() {
    this.readyState = 1;
    this.onopen?.({});
  }
  serverClose(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code, reason: 'gone' });
  }
  push(obj: unknown) {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  json() {
    return this.sent.map((s) => JSON.parse(s) as { type: string; id: string; ts_ms: number; data: Record<string, unknown> });
  }
}

afterEach(() => {
  vi.useRealTimers();
  FakeWs.all = [];
});

const hb: HeartbeatData = {
  state: 'PLAYING',
  current: { creative_code: 'RD-A7M2P9', started_at_ms: 1 },
  manifest_version_applied: 42,
  manifest_version_pending: null,
  download: null,
  last_sync_ms: 1,
  last_play_ms: 1,
  storage: { free_bytes: 1, used_bytes: 1, persistent: true },
  clock_offset_ms: 0,
  alerts: [],
};
const tel = { app_version: '1.0.0', platform: 'web' } as TelemetryData;

describe('envelope do WS', () => {
  it('{type,id,ts_ms,data} com tipo minúsculo', () => {
    const e = makeEnvelope('heartbeat', { a: 1 }, 1791380000123.7, 'id-1');
    expect(e).toEqual({ type: 'heartbeat', id: 'id-1', ts_ms: 1791380000123, data: { a: 1 } });
  });

  it('parse tolerante; rejeita tipos maiúsculos (protocolo antigo) e lixo', () => {
    expect(parseEnvelope('{"type":"kill","id":"x","ts_ms":1,"data":{"creative_ids":["a"]}}')?.data).toEqual({ creative_ids: ['a'] });
    expect(parseEnvelope({ type: 'flush_events' })).toEqual({ type: 'flush_events', id: '', ts_ms: 0, data: {} });
    expect(parseEnvelope('{"type":"KILL_COMMAND"}')).toBeNull();
    expect(parseEnvelope('não-json')).toBeNull();
    expect(parseEnvelope('[1]')).toBeNull();
  });
});

describe('reconexão com backoff exponencial + jitter (1 s → 60 s)', () => {
  it('pede ticket novo a cada conexão e respeita os limites do backoff', async () => {
    vi.useFakeTimers();
    let tickets = 0;
    const sock = new ReconnectingSocket({
      url: async () => `ws://x/ws/player?ticket=t${++tickets}`,
      factory: (u) => new FakeWs(u),
      random: () => 0.999,
    });
    sock.start();
    for (let i = 0; i < 9; i++) {
      await vi.advanceTimersByTimeAsync(0);
      FakeWs.all.at(-1)!.serverClose();
      await vi.advanceTimersByTimeAsync(sock.scheduledDelays.at(-1)!);
    }
    expect(FakeWs.all.map((w) => w.url).slice(0, 3)).toEqual(['ws://x/ws/player?ticket=t1', 'ws://x/ws/player?ticket=t2', 'ws://x/ws/player?ticket=t3']);
    const d = sock.scheduledDelays;
    const caps = [1_000, 2_000, 4_000, 8_000, 16_000, 32_000, 60_000, 60_000, 60_000];
    d.forEach((ms, i) => {
      expect(ms).toBeGreaterThanOrEqual(caps[i]! / 2);
      expect(ms).toBeLessThanOrEqual(caps[i]!);
    });
    // conexão bem-sucedida zera o backoff
    await vi.advanceTimersByTimeAsync(0);
    FakeWs.all.at(-1)!.open();
    FakeWs.all.at(-1)!.serverClose();
    expect(sock.scheduledDelays.at(-1)!).toBeLessThanOrEqual(1_000);
    sock.stop();
  });

  it('falha ao obter o ticket também entra no backoff (sem metralhadora)', async () => {
    vi.useFakeTimers();
    let calls = 0;
    const sock = new ReconnectingSocket({
      url: async () => {
        calls++;
        throw new Error('offline');
      },
      factory: (u) => new FakeWs(u),
      random: () => 0,
    });
    sock.start();
    await vi.advanceTimersByTimeAsync(10_000);
    // 0 + 500 + 1000 + 2000 + 4000 ≤ 10 s ⇒ no máximo 5–6 tentativas
    expect(calls).toBeLessThanOrEqual(6);
    sock.stop();
  });
});

describe('canal do player: heartbeat 30 s, telemetria 5 min, mensagens do servidor', () => {
  function channel(handlers: Partial<ChannelHandlers> = {}) {
    let n = 0;
    return new PlayerChannel({
      url: async () => 'ws://x/ws/player?ticket=t',
      factory: (u) => new FakeWs(u),
      handlers: { heartbeat: () => hb, telemetry: async () => tel, ...handlers },
      settings: () => ({ heartbeat_interval_ms: 30_000, telemetry_interval_ms: 300_000 }),
      now: () => 1791380000000,
      uuid: () => `u${++n}`,
    });
  }

  it('heartbeat imediato + a cada 30 s; telemetria imediata + a cada 5 min; para ao desconectar', async () => {
    vi.useFakeTimers();
    const ch = channel();
    ch.start();
    await vi.advanceTimersByTimeAsync(0);
    const ws = FakeWs.all[0]!;
    ws.open();
    await vi.advanceTimersByTimeAsync(10 * 60_000);
    const types = ws.json().map((e) => e.type);
    expect(types.filter((t) => t === 'heartbeat')).toHaveLength(1 + 20);
    expect(types.filter((t) => t === 'telemetry')).toHaveLength(1 + 2);
    const first = ws.json()[0]!;
    expect(first).toMatchObject({ type: 'heartbeat', id: 'u1', ts_ms: 1791380000000, data: hb });
    ws.serverClose();
    const before = ws.sent.length;
    await vi.advanceTimersByTimeAsync(60_000);
    expect(ws.sent.length).toBe(before);
    ch.stop();
  });

  it('despacha command/kill/device_revoked/server_time/sync_required/flush_events', async () => {
    vi.useFakeTimers();
    const got: string[] = [];
    let cmd: CommandData | null = null;
    const ch = channel({
      onCommand: (c) => {
        cmd = c;
        got.push('command');
      },
      onKill: (k) => got.push(`kill:${k.creative_ids.join(',')}`),
      onRevoked: () => got.push('revoked'),
      onServerTime: (ms) => got.push(`time:${ms}`),
      onSyncRequired: (v) => got.push(`sync:${v}`),
      onFlush: () => got.push('flush'),
    });
    ch.start();
    await vi.advanceTimersByTimeAsync(0);
    const ws = FakeWs.all[0]!;
    ws.open();
    ws.push({ type: 'command', id: '1', ts_ms: 1, data: { command_id: 'c1', command: 'resync', args: {}, expires_at_ms: 5 } });
    ws.push({ type: 'kill', id: '2', ts_ms: 1, data: { creative_ids: ['a', 'b'], reason: 'jurídico' } });
    ws.push({ type: 'server_time', id: '3', ts_ms: 1, data: { server_time_ms: 123 } });
    ws.push({ type: 'sync_required', id: '4', ts_ms: 1, data: { version: 43 } });
    ws.push({ type: 'flush_events', id: '5', ts_ms: 1, data: {} });
    ws.push({ type: 'device_revoked', id: '6', ts_ms: 1, data: {} });
    expect(got).toEqual(['command', 'kill:a,b', 'time:123', 'sync:43', 'flush', 'revoked']);
    expect(cmd).toEqual({ command_id: 'c1', command: 'resync', args: {}, expires_at_ms: 5 });
    ch.stop();
  });
});

describe('kill persistido e comandos idempotentes', () => {
  it('kill é gravado antes de notificar e sobrevive a reboot', async () => {
    const store = new MemoryStateStore();
    const k1 = new KillList(store);
    await k1.load();
    const seen: string[][] = [];
    k1.onChange((ids) => seen.push(ids));
    expect(await k1.add(['cr-1', 'cr-2'])).toEqual(['cr-1', 'cr-2']);
    expect(await k1.add(['cr-2'])).toEqual([]);
    expect(seen).toEqual([['cr-1', 'cr-2']]);
    const k2 = new KillList(store);
    await k2.load();
    expect(k2.has('cr-1') && k2.has('cr-2')).toBe(true);
  });

  it('kill local é liberado só por manifesto gerado DEPOIS dele e sem o criativo em revoked (lift no painel)', async () => {
    const store = new MemoryStateStore();
    let now = 1_000_000;
    const k = new KillList(store, () => now);
    await k.load();
    await k.add(['cr-1', 'cr-2']);
    // manifesto gerado ANTES do kill (chegou atrasado): não libera
    expect(await k.reconcile([], 999_000)).toEqual([]);
    // manifesto gerado depois, ainda com cr-1 revogado: libera só cr-2
    now = 2_000_000;
    expect(await k.reconcile(['cr-1'], 1_500_000)).toEqual(['cr-2']);
    expect(k.ids()).toEqual(['cr-1']);
    // dentro da margem de relógio: não libera
    expect(await k.reconcile([], 1_000_000 + 1_500)).toEqual([]);
    // persiste entre reboots
    const k2 = new KillList(store, () => now);
    await k2.load();
    expect(k2.ids()).toEqual(['cr-1']);
    expect(await k2.reconcile([], 1_600_000)).toEqual(['cr-1']);
    expect(k2.ids()).toEqual([]);
  });

  it('RECEIVED → DONE; reentrega não reexecuta; expirado ⇒ FAILED; desconhecido ⇒ UNSUPPORTED', async () => {
    const store = new MemoryStateStore();
    const acks: string[] = [];
    let runs = 0;
    const cp = new CommandProcessor({
      store,
      now: () => 1_000,
      send: (_t, d) => {
        acks.push(`${d.command_id}:${d.status}`);
        return true;
      },
      handlers: {
        resync: async () => {
          runs++;
          return { status: 'DONE' };
        },
      },
    });
    const c = (id: string, command: string, exp: number | null = null): CommandData => ({ command_id: id, command, args: null, expires_at_ms: exp });
    await cp.handle(c('a', 'resync'));
    await cp.handle(c('a', 'resync'));
    await cp.handle(c('b', 'resync', 999));
    await cp.handle(c('d', 'formatar_disco'));
    expect(runs).toBe(1);
    expect(acks).toEqual(['a:RECEIVED', 'a:DONE', 'a:DONE', 'b:FAILED', 'd:UNSUPPORTED']);
  });

  it('restart_app: persiste PENDING_RESTART, reinicia e envia DONE ao voltar (sem reexecutar)', async () => {
    const store = new MemoryStateStore();
    let restarted = 0;
    const acks: string[] = [];
    const mk = () =>
      new CommandProcessor({
        store,
        now: () => 1_000,
        send: (_t, d) => {
          acks.push(`${d.command_id}:${d.status}`);
          return true;
        },
        handlers: { restart_app: async () => ({ status: 'RESTARTING', restart: () => void restarted++ }) },
      });
    await mk().handle({ command_id: 'r1', command: 'restart_app', args: null, expires_at_ms: null });
    expect(restarted).toBe(1);
    expect(acks).toEqual(['r1:RECEIVED']);
    // após o reinício (nova instância)
    const after = mk();
    await after.resumePending();
    await after.handle({ command_id: 'r1', command: 'restart_app', args: null, expires_at_ms: null }); // reentrega
    expect(restarted).toBe(1);
    expect(acks).toEqual(['r1:RECEIVED', 'r1:DONE', 'r1:DONE']);
  });
});
