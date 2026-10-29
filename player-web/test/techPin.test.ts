import { pbkdf2Sync, randomBytes } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { CommandProcessor } from '../src/core/commands';
import { MemoryStateStore } from '../src/core/memoryStores';
import { parseTechPinArgs, TechPinStore } from '../src/core/techPin';
import { b64uEncode } from '../src/core/util';

/** Igual ao backend (modules/playback/commands.py: tech_pin_args): PBKDF2-SHA256, 210 000 it., sal 16 B, hash 32 B. */
function serverArgs(pin: string) {
  const salt = randomBytes(16);
  const hash = pbkdf2Sync(pin, salt, 210_000, 32, 'sha256');
  return { algo: 'pbkdf2-sha256', iterations: 210_000, salt_b64u: b64uEncode(salt), hash_b64u: b64uEncode(hash) };
}

describe('set_tech_pin (RDPP §4)', () => {
  it('guarda só sal+hash e verifica o PIN com PBKDF2 do WebCrypto (interoperável com o backend)', async () => {
    const st = new MemoryStateStore();
    const pins = new TechPinStore(st);
    expect(await pins.verify('123456')).toBeNull();
    await pins.set(serverArgs('482913'), 1);
    expect(await pins.verify('482913')).toBe(true);
    expect(await pins.verify('482914')).toBe(false);
    expect(JSON.stringify([...st.kv.values()])).not.toContain('482913');
  });

  it('rejeita PIN em claro, algoritmo/iterações/tamanhos inválidos', () => {
    const ok = serverArgs('123456');
    expect(() => parseTechPinArgs({ ...ok, pin: '123456' }, 0)).toThrow(/claro/);
    expect(() => parseTechPinArgs({ ...ok, algo: 'sha1' }, 0)).toThrow(/algoritmo/);
    expect(() => parseTechPinArgs({ ...ok, iterations: 1000 }, 0)).toThrow(/iterations/);
    expect(() => parseTechPinArgs({ ...ok, hash_b64u: b64uEncode(new Uint8Array(16)) }, 0)).toThrow(/tamanho/);
    expect(() => parseTechPinArgs(null, 0)).toThrow(/args/);
  });

  it('comando via CommandProcessor: RECEIVED → DONE; args inválidos ⇒ FAILED', async () => {
    const st = new MemoryStateStore();
    const pins = new TechPinStore(st);
    const acks: string[] = [];
    const cp = new CommandProcessor({
      store: st,
      now: () => 1,
      send: (_t, d) => {
        acks.push(`${d.command_id}:${d.status}`);
        return true;
      },
      handlers: {
        set_tech_pin: async (cmd) => {
          await pins.set(cmd.args, 1);
          return { status: 'DONE' };
        },
      },
    });
    await cp.handle({ command_id: 'p1', command: 'set_tech_pin', args: serverArgs('777777'), expires_at_ms: null });
    await cp.handle({ command_id: 'p2', command: 'set_tech_pin', args: { pin: '777777' }, expires_at_ms: null });
    expect(acks).toEqual(['p1:RECEIVED', 'p1:DONE', 'p2:RECEIVED', 'p2:FAILED']);
    expect(await pins.verify('777777')).toBe(true);
  });
});
