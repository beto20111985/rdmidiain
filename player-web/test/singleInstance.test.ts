import { describe, expect, it } from 'vitest';
import { acquireSingleInstance, type LockManagerLike } from '../src/adapters/web/singleInstance';

/** LockManager falso (semântica da Web Locks API para um único nome exclusivo). */
class FakeLocks implements LockManagerLike {
  private held = false;
  private queue: (() => void)[] = [];
  release(): void {
    this.held = false;
    this.queue.shift()?.();
  }
  async request(_n: string, o: { ifAvailable?: boolean }, cb: (lock: unknown) => Promise<unknown>): Promise<unknown> {
    if (this.held) {
      if (o.ifAvailable) return cb(null);
      await new Promise<void>((r) => this.queue.push(r));
    }
    this.held = true;
    return cb({ name: 'x' });
  }
}

describe('instância única por origem (Web Locks)', () => {
  it('primeira aba adquire; segunda espera (mensagem) e assume quando a primeira fecha', async () => {
    const locks = new FakeLocks();
    expect(await acquireSingleInstance(locks, () => undefined)).toBe('ACQUIRED');
    let waiting = 0;
    let second: string | null = null;
    const p = acquireSingleInstance(locks, () => void waiting++).then((r) => (second = r));
    await new Promise((r) => setTimeout(r, 10));
    expect(waiting).toBe(1);
    expect(second).toBeNull();
    locks.release(); // primeira aba fechou
    await p;
    expect(second).toBe('WAITED');
  });

  it('sem Web Locks: roda sem proteção (UNSUPPORTED)', async () => {
    expect(await acquireSingleInstance(undefined, () => undefined)).toBe('UNSUPPORTED');
  });
});
