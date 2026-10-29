import { afterEach, describe, expect, it, vi } from 'vitest';
import { createResolveContext } from '../src/core/cycle';
import { PlaybackEngine, type MediaEvent, type PlaybackContext, type PreparedHandle, type Renderer, type RenderItem } from '../src/core/playback';
import type { PopInput } from '../src/core/pop';
import type { Manifest } from '../src/core/protocol';
import { ALL_MEDIA, ANCHOR_0807 as A, manaus, MIN, mk, SEC } from './helpers/manifest';

class FakeRenderer implements Renderer {
  shows: { t: number; key: string | null; footer: string | null }[] = [];
  prepareDelay = new Map<string, number>();
  stallVideo = new Set<string>();
  failDecode = new Set<string>();
  resets = 0;
  private listeners = new Map<PreparedHandle, (ev: MediaEvent) => void>();

  async prepare(item: RenderItem): Promise<PreparedHandle> {
    const d = this.prepareDelay.get(item.creativeKey);
    if (d) await new Promise((r) => setTimeout(r, d));
    if (this.failDecode.has(item.creativeKey)) throw new Error('decode');
    return { item };
  }
  builtin(): PreparedHandle {
    return { item: null };
  }
  show(h: PreparedHandle, o: { fadeMs: number; footerText: string | null }): void {
    this.shows.push({ t: Date.now(), key: h.item?.creativeKey ?? null, footer: o.footerText });
    const it = h.item;
    if (it?.isVideo && !this.stallVideo.has(it.creativeKey)) {
      setTimeout(() => this.listeners.get(h)?.('ended'), it.mediaDurationMs ?? 0);
    }
  }
  discard(): void {}
  onMediaEvent(h: PreparedHandle, cb: (ev: MediaEvent) => void): () => void {
    this.listeners.set(h, cb);
    return () => this.listeners.delete(h);
  }
  reset(): void {
    this.resets++;
  }
  clear(): void {}
}

interface Harness {
  engine: PlaybackEngine;
  renderer: FakeRenderer;
  pops: PopInput[];
  kills: Set<string>;
  commits: number[];
  mediaErrors: string[];
  setStaged(m: Manifest | null): void;
  setOffset(ms: number): void;
  setActive(m: Manifest | null): void;
  refresh(): void;
  degraded(): void;
}

function harness(start: number, m: Manifest | null = mk(), media: string[] = ALL_MEDIA): Harness {
  vi.useFakeTimers();
  vi.setSystemTime(start);
  const kills = new Set<string>();
  let active = m;
  let staged: Manifest | null = null;
  const ctxOf = (man: Manifest): PlaybackContext => ({
    manifest: man,
    version: man.version,
    resolve: createResolveContext(man, A, { revoked: [...kills], media_ready: media }),
  });
  let ctx: PlaybackContext | null = active ? ctxOf(active) : null;
  const pops: PopInput[] = [];
  const commits: number[] = [];
  const mediaErrors: string[] = [];
  const renderer = new FakeRenderer();
  const clock = { off: 0, now: () => Date.now() + clock.off, get offsetMs() { return clock.off; } };
  const engine = new PlaybackEngine({
    renderer,
    clock,
    mono: () => Date.now(),
    getContext: () => ctx,
    peekStagedContext: () => (staged ? ctxOf(staged) : null),
    hasStaged: () => staged !== null,
    commitStaged: async () => {
      commits.push(Date.now());
      active = staged;
      staged = null;
      ctx = active ? ctxOf(active) : null;
      return true;
    },
    mediaFor: async (c, key) => {
      const cr = c.manifest.creatives[key];
      const me = cr ? c.manifest.media[cr.media] : undefined;
      if (!cr || !me || !c.resolve.mediaReady.has(cr.media)) return null;
      return {
        creativeKey: key,
        mediaKey: cr.media,
        sha256: me.sha256,
        mime: me.mime,
        isVideo: me.mime.startsWith('video/'),
        mediaDurationMs: me.duration_ms,
        url: `blob:${key}`,
        label: cr.label,
        code: cr.code,
        release: () => undefined,
      };
    },
    pop: { record: async (i) => void pops.push(i) },
    isOffline: () => false,
    onMediaError: (k, r) => mediaErrors.push(`${k}:${r}`),
  });
  return {
    engine,
    renderer,
    pops,
    kills,
    commits,
    mediaErrors,
    setStaged: (s) => (staged = s),
    setOffset: (ms) => (clock.off = ms),
    setActive: (s) => {
      active = s;
      ctx = s ? ctxOf(s) : null;
    },
    refresh: () => {
      ctx = active ? ctxOf(active) : null;
      engine.refresh();
    },
    degraded: () => engine.markDegraded('hidden'),
  };
}

afterEach(async () => {
  vi.useRealTimers();
});

const keyed = (pops: PopInput[]) => pops.map((p) => `${p.creative.code}:${p.result}`);

describe('runtime — agenda absoluta e PoP', () => {
  it('boot exatamente na fronteira (08:47): toca o ciclo inteiro com PoP contíguo e COMPLETED', async () => {
    const h = harness(manaus(8, 47));
    h.engine.start();
    await vi.advanceTimersByTimeAsync(4 * MIN + 1);
    expect(h.renderer.shows.map((s) => [s.t - manaus(8, 47), s.key])).toEqual([
      [0, 'c7'],
      [60_000, 'c8'],
      [120_000, 'c1'],
      [150_000, 'c2'],
      [180_000, 'f1'],
      [210_000, 'f1'],
      [240_000, 'c7'],
    ]);
    expect(h.renderer.shows[2]!.footer).toBe('Publicidade — RD-C1AAAA');
    expect(h.renderer.shows[0]!.footer).toBe('Conteúdo do estabelecimento — RD-C7CCCC');
    expect(h.pops.map((p) => [p.creative.code, p.result, p.actual_ms, p.expected_ms])).toEqual([
      ['RD-C7CCCC', 'COMPLETED', 60_000, 60_000],
      ['RD-C8DDDD', 'COMPLETED', 60_000, 60_000],
      ['RD-C1AAAA', 'COMPLETED', 30_000, 30_000],
      ['RD-C2BBBB', 'COMPLETED', 30_000, 30_000],
      ['RD-F1EEEE', 'COMPLETED', 30_000, 30_000],
      ['RD-F1EEEE', 'COMPLETED', 30_000, 30_000],
    ]);
    // intervalos contíguos: fim do anterior = início visível do seguinte (§8.2.10)
    for (let i = 1; i < h.pops.length; i++) {
      expect(h.pops[i]!.started_at_ms).toBe(h.pops[i - 1]!.started_at_ms + h.pops[i - 1]!.actual_ms);
    }
    expect(h.pops[2]).toMatchObject({ insertion_id: expect.stringMatching(/^ins-/), creative: { kind: 'EXTERNAL', campaign_id: 'camp-c1aaaa' } });
    await h.engine.stop();
  });

  it('reboot às 08:49:10 (meio do item): FILLER até 08:49:30 + SKIPPED do item perdido; âncora não muda', async () => {
    const h = harness(manaus(8, 49, 10));
    h.engine.start();
    await vi.advanceTimersByTimeAsync(31 * SEC);
    expect(h.renderer.shows.map((s) => [s.t - manaus(8, 49), s.key])).toEqual([
      [10_000, 'f1'], // FILLER (fallback, sem PoP)
      [30_000, 'c2'],
    ]);
    const skipped = h.pops.filter((p) => p.result === 'SKIPPED');
    expect(skipped).toHaveLength(1);
    expect(skipped[0]).toMatchObject({
      creative: { code: 'RD-C1AAAA' },
      started_at_ms: manaus(8, 49, 10),
      actual_ms: 0,
      expected_ms: 30_000,
    });
    // o FILLER não gerou PoP
    expect(h.pops.filter((p) => p.creative.code === 'RD-F1EEEE')).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(30 * SEC);
    expect(keyed(h.pops)).toEqual(['RD-C1AAAA:SKIPPED', 'RD-C2BBBB:COMPLETED']);
    await h.engine.stop();
  });

  it('entrada dentro da tolerância (≤ 1,5 s após o início) toca o item; atraso absorvido sem cortar o externo', async () => {
    const h = harness(manaus(8, 49, 0, 800));
    h.engine.start();
    await vi.advanceTimersByTimeAsync(2 * MIN);
    const c1 = h.pops.find((p) => p.creative.code === 'RD-C1AAAA')!;
    expect(c1).toMatchObject({ result: 'COMPLETED', actual_ms: 30_000, started_at_ms: manaus(8, 49, 0, 800) });
    const c2 = h.pops.find((p) => p.creative.code === 'RD-C2BBBB')!;
    expect(c2).toMatchObject({ result: 'COMPLETED', actual_ms: 30_000, started_at_ms: manaus(8, 49, 30, 800) });
    // o fallback (flexível) absorve os 800 ms e a grade volta ao horário em 08:50:30
    const f = h.pops.filter((p) => p.creative.code === 'RD-F1EEEE');
    expect(f[0]).toMatchObject({ result: 'INTERRUPTED', actual_ms: 29_200 });
    expect(f[1]).toMatchObject({ result: 'COMPLETED', started_at_ms: manaus(8, 50, 30) });
    await h.engine.stop();
  });

  it('pré-carga lenta de um externo: o anterior continua visível; o externo nunca é cortado', async () => {
    const h = harness(manaus(8, 48));
    h.renderer.prepareDelay.set('c1', 61_000 - 300); // pronto 300 ms depois do horário
    h.engine.start();
    await vi.advanceTimersByTimeAsync(2 * MIN);
    const c8 = h.pops.find((p) => p.creative.code === 'RD-C8DDDD')!;
    const c1 = h.pops.find((p) => p.creative.code === 'RD-C1AAAA')!;
    expect(c8.result).toBe('COMPLETED');
    expect(c8.actual_ms).toBeGreaterThanOrEqual(60_000);
    expect(c1).toMatchObject({ result: 'COMPLETED', actual_ms: 30_000 });
    expect(c1.started_at_ms).toBe(c8.started_at_ms + c8.actual_ms);
    await h.engine.stop();
  });
});

describe('runtime — kill, emergência, troca de manifesto', () => {
  it('kill durante o anúncio: interrompe na hora (INTERRUPTED), FILLER até a fronteira, sem SKIPPED', async () => {
    const h = harness(manaus(8, 49));
    h.engine.start();
    await vi.advanceTimersByTimeAsync(5 * SEC);
    h.kills.add('cr-c1aaaa');
    h.refresh();
    await vi.advanceTimersByTimeAsync(30 * SEC);
    expect(h.renderer.shows.map((s) => [s.t - manaus(8, 49), s.key])).toEqual([
      [0, 'c1'],
      [5_000, 'f1'],
      [30_000, 'c2'],
    ]);
    expect(keyed(h.pops)).toEqual(['RD-C1AAAA:INTERRUPTED']);
    expect(h.pops[0]!.actual_ms).toBe(5_000);
    // nos ciclos seguintes o criativo não volta (persistência do kill é do KillList)
    await vi.advanceTimersByTimeAsync(4 * MIN);
    expect(h.renderer.shows.filter((s) => s.key === 'c1')).toHaveLength(1);
    await h.engine.stop();
  });

  it('emergência: item que seria preemptado não começa (SKIPPED); itens normais dentro da janela ⇒ SKIPPED; volta à grade', async () => {
    const X = manaus(8, 49, 10);
    const h = harness(manaus(8, 48), mk({ emergency: [{ id: 'e1', c: 'c2', from_ms: X, until_ms: X + 60 * SEC }] }));
    h.engine.start();
    await vi.advanceTimersByTimeAsync(2 * MIN + 31 * SEC);
    expect(h.renderer.shows.map((s) => [s.t - manaus(8, 48), s.key])).toEqual([
      [0, 'c8'],
      [60_000, 'f1'], // FILLER 08:49:00–08:49:10 (c1 preemptado)
      [70_000, 'c2'], // emergência 08:49:10
      [100_000, 'c2'], // emergência 08:49:40
      [130_000, 'f1'], // retorno às 08:50:10 no meio do fallback ⇒ FILLER até 08:50:30
      [150_000, 'f1'], // 08:50:30 fallback normal
    ]);
    const skipped = h.pops.filter((p) => p.result === 'SKIPPED').map((p) => p.creative.code);
    expect(skipped).toEqual(['RD-C1AAAA', 'RD-C2BBBB']); // c1 08:49:00 e c2 regular 08:49:30; fallback não gera SKIPPED
    const em = h.pops.filter((p) => p.result !== 'SKIPPED' && p.creative.code === 'RD-C2BBBB');
    expect(em.map((p) => [p.result, p.insertion_id, p.actual_ms])).toEqual([
      ['COMPLETED', null, 30_000],
      ['COMPLETED', null, 30_000],
    ]);
    await h.engine.stop();
  });

  it('nova versão staged só é aplicada na fronteira de ciclo (fase == 0)', async () => {
    const h = harness(manaus(8, 47));
    h.engine.start();
    await vi.advanceTimersByTimeAsync(30 * SEC);
    const v43 = mk({ version: 43, internalPattern: [[{ c: 'c8', d: 120_000 }]] });
    h.setStaged(v43);
    await vi.advanceTimersByTimeAsync(4 * MIN);
    expect(h.commits).toEqual([manaus(8, 51)]);
    const at = (t: number) => h.renderer.shows.find((s) => s.t === t)?.key;
    expect(at(manaus(8, 48))).toBe('c8'); // v42 ainda
    expect(at(manaus(8, 49))).toBe('c1');
    expect(at(manaus(8, 51))).toBe('c8'); // v43: padrão interno novo a partir da fronteira
    const after = h.pops.filter((p) => p.started_at_ms >= manaus(8, 51));
    expect(after.every((p) => p.manifest_version === 43)).toBe(true);
    const before = h.pops.filter((p) => p.started_at_ms < manaus(8, 51));
    expect(before.every((p) => p.manifest_version === 42)).toBe(true);
    await h.engine.stop();
  });
});

describe('runtime — falhas de mídia, vídeo e visibilidade', () => {
  it('vídeo sem evento de fim do decodificador ⇒ INTERRUPTED após a carência; o seguinte (flexível) realinha', async () => {
    const h = harness(manaus(8, 47));
    h.renderer.stallVideo.add('c7');
    h.engine.start();
    await vi.advanceTimersByTimeAsync(2 * MIN + 1);
    expect(h.pops[0]).toMatchObject({ creative: { code: 'RD-C7CCCC' }, result: 'INTERRUPTED', actual_ms: 61_500 });
    expect(h.pops[1]).toMatchObject({ creative: { code: 'RD-C8DDDD' }, result: 'INTERRUPTED', actual_ms: 58_500 });
    expect(h.renderer.shows.find((s) => s.key === 'c1')!.t).toBe(manaus(8, 49));
    await h.engine.stop();
  });

  it('mídia indisponível para um item ⇒ tela institucional embutida + evento ERROR + media_error', async () => {
    const h = harness(manaus(8, 49), mk(), ALL_MEDIA);
    h.renderer.failDecode.add('c1');
    h.engine.start();
    await vi.advanceTimersByTimeAsync(31 * SEC);
    expect(h.renderer.shows.map((s) => s.key)).toEqual([null, 'c2']);
    expect(h.renderer.shows[0]!.footer).toBe('RD Mídia Indoor');
    expect(keyed(h.pops)).toEqual(['RD-C1AAAA:ERROR']);
    expect(h.pops[0]).toMatchObject({ actual_ms: 0 });
    expect(h.mediaErrors[0]).toMatch(/^c1:DECODE/);
    await h.engine.stop();
  });

  it('página oculta/quadro congelado durante o item ⇒ nunca COMPLETED', async () => {
    const h = harness(manaus(8, 49));
    h.engine.start();
    await vi.advanceTimersByTimeAsync(10 * SEC);
    h.degraded();
    await vi.advanceTimersByTimeAsync(21 * SEC);
    expect(h.pops[0]).toMatchObject({ creative: { code: 'RD-C1AAAA' }, result: 'INTERRUPTED', actual_ms: 30_000 });
    await h.engine.stop();
  });

  it('sem manifesto: tela institucional; ao ativar o primeiro, entra na grade com a regra de meio de item', async () => {
    const h = harness(manaus(8, 49, 10), null);
    h.engine.start();
    await vi.advanceTimersByTimeAsync(2 * SEC);
    expect(h.renderer.shows.map((s) => s.key)).toEqual([null]);
    h.setActive(mk());
    h.engine.refresh();
    await vi.advanceTimersByTimeAsync(20 * SEC);
    expect(h.renderer.shows.map((s) => [s.t - manaus(8, 49), s.key])).toEqual([
      [10_000, null],
      [12_000, 'f1'],
      [30_000, 'c2'],
    ]);
    expect(keyed(h.pops)).toEqual(['RD-C1AAAA:SKIPPED']);
    await h.engine.stop();
  });

  it('watchdog: recover() recria camadas e reentra (item atual INTERRUPTED)', async () => {
    const h = harness(manaus(8, 49));
    h.engine.start();
    await vi.advanceTimersByTimeAsync(3 * SEC);
    h.engine.recover('FROZEN');
    await vi.advanceTimersByTimeAsync(30 * SEC);
    expect(h.renderer.resets).toBe(1);
    expect(h.pops[0]).toMatchObject({ creative: { code: 'RD-C1AAAA' }, result: 'INTERRUPTED', actual_ms: 3_000 });
    expect(h.renderer.shows.map((s) => s.key)).toEqual(['c1', 'f1', 'c2']);
    await h.engine.stop();
  });
});

describe('runtime — correção do relógio (achado do E2E real)', () => {
  it('correção para trás do offset durante a tela institucional NÃO desloca a cadeia de PoP nem corta itens', async () => {
    // Boot sem manifesto com o relógio corrigido 2 480 ms adiantado (1ª amostra ruim); a amostra seguinte corrige.
    const h = harness(manaus(8, 46, 50), null);
    h.setOffset(2_480);
    h.engine.start();
    await vi.advanceTimersByTimeAsync(7_000);
    h.setOffset(0);
    h.setActive(mk());
    h.engine.refresh();
    await vi.advanceTimersByTimeAsync(3 * MIN);
    const pops = h.pops.filter((p) => p.result !== 'SKIPPED');
    if (process.env.DBG) {
      const f = (t: number) => new Date(t).toISOString().slice(14, 23);
      console.log(h.pops.map((p) => `${p.creative.code} ${p.result} ${p.actual_ms} ${f(p.started_at_ms)}`).join('\n'));
      console.log('shows ' + h.renderer.shows.map((s) => `${f(s.t)} ${s.key}`).join(' | '));
    }
    expect(pops.length).toBeGreaterThanOrEqual(3);
    for (const p of pops) {
      // início alinhado à grade (âncora 08:07:00, unidades de 30 s) no relógio corrigido atual
      expect((p.started_at_ms - A) % 30_000, p.creative.code + '@' + p.started_at_ms).toBe(0);
      expect(p.result, p.creative.code).toBe('COMPLETED');
    }
    await h.engine.stop();
  });

  it('jitter do offset (±40 ms) não encurta itens: todos COMPLETED, intervalos contíguos sem sobreposição', async () => {
    const h = harness(manaus(8, 47));
    h.engine.start();
    const jitter = [40, -35, 25, -40, 10, -20, 35, -5];
    for (const j of jitter) {
      await vi.advanceTimersByTimeAsync(29_000);
      h.setOffset(j);
    }
    await vi.advanceTimersByTimeAsync(40_000);
    const pops = h.pops.filter((p) => p.result !== 'SKIPPED');
    expect(pops.length).toBeGreaterThanOrEqual(6);
    for (const p of pops) {
      expect(p.result, `${p.creative.code} ${p.actual_ms}`).toBe('COMPLETED');
      expect(p.actual_ms).toBeGreaterThanOrEqual(p.expected_ms);
    }
    for (let i = 1; i < pops.length; i++) {
      expect(pops[i]!.started_at_ms).toBeGreaterThanOrEqual(pops[i - 1]!.started_at_ms + pops[i - 1]!.actual_ms);
    }
    await h.engine.stop();
  });

  it('correção grande para trás no meio da cadeia: um item cede a sobreposição (honesto) e a cadeia volta à grade', async () => {
    const h = harness(manaus(8, 47));
    h.engine.start();
    await vi.advanceTimersByTimeAsync(45_000);
    h.setOffset(-3_000); // relógio corrigido volta 3 s
    await vi.advanceTimersByTimeAsync(3 * MIN);
    const pops = h.pops.filter((p) => p.result !== 'SKIPPED');
    for (let i = 1; i < pops.length; i++) {
      expect(pops[i]!.started_at_ms, 'sem sobreposição').toBeGreaterThanOrEqual(pops[i - 1]!.started_at_ms + pops[i - 1]!.actual_ms);
    }
    const tail = pops.slice(-4);
    for (const p of tail) {
      expect((p.started_at_ms - A) % 30_000, 'realinhado à grade').toBe(0);
      expect(p.result).toBe('COMPLETED');
    }
    await h.engine.stop();
  });
});
