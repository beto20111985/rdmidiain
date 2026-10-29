/**
 * Renderer DOM com DOUBLE BUFFER A/B (RDPP §5.1.9; ETAPA2 #1197–#1209):
 *  - o próximo item é pré-carregado na camada de trás (vídeo até `canplay`, imagem com `decode()`);
 *  - a troca é um crossfade de `fade_ms` com a camada nova POR CIMA da antiga (a antiga continua opaca por baixo
 *    até o fim do fade ⇒ nunca quadro preto);
 *  - letterbox (`object-fit: contain`), nunca esticar nem cortar; vídeo mudo, inline, sem controles; ao terminar,
 *    o <video> mantém o último quadro;
 *  - tela institucional embutida para FILLER sem fallback/mídia indisponível.
 */

import type { MediaEvent, PreparedHandle, Renderer, RenderItem } from '../core/playback';
import type { FooterView } from './footer';

interface Layer {
  el: HTMLDivElement;
  video: HTMLVideoElement;
  img: HTMLImageElement;
  item: RenderItem | null;
  gen: number;
  /** performance.now() em que o fade-out desta camada termina. */
  busyUntil: number;
  hideTimer: ReturnType<typeof setTimeout> | null;
}

class DomHandle implements PreparedHandle {
  constructor(
    readonly item: RenderItem | null,
    readonly layer: Layer | null,
    readonly gen: number,
  ) {}
}

const PREPARE_TIMEOUT_MS = 15_000;

function waitVideoReady(v: HTMLVideoElement, timeoutMs = PREPARE_TIMEOUT_MS): Promise<void> {
  if (v.readyState >= 3) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const cleanup = (): void => {
      clearTimeout(t);
      v.removeEventListener('canplay', ok);
      v.removeEventListener('error', bad);
    };
    const ok = (): void => {
      cleanup();
      resolve();
    };
    const bad = (): void => {
      cleanup();
      reject(new Error(`vídeo: ${v.error?.message || `código ${v.error?.code ?? '?'}`}`));
    };
    const t = setTimeout(() => {
      cleanup();
      reject(new Error('vídeo: tempo esgotado na pré-carga'));
    }, timeoutMs);
    v.addEventListener('canplay', ok);
    v.addEventListener('error', bad);
  });
}

export class DomRenderer implements Renderer {
  private layers: [Layer, Layer];
  private builtinEl: HTMLDivElement;
  private front: Layer | 'builtin' | null = null;
  private z = 1;
  /** Erros de reprodução (autoplay bloqueado etc.) para diagnóstico. */
  onPlaybackIssue: ((msg: string) => void) | null = null;

  constructor(
    private readonly stage: HTMLElement,
    private readonly footer: FooterView,
    private readonly doc: Document = document,
  ) {
    this.layers = [this.createLayer(), this.createLayer()];
    this.builtinEl = this.createBuiltin();
  }

  private createLayer(): Layer {
    const el = this.doc.createElement('div');
    el.className = 'layer';
    const video = this.doc.createElement('video');
    video.muted = true;
    video.defaultMuted = true;
    video.playsInline = true;
    video.preload = 'auto';
    video.controls = false;
    video.loop = false;
    video.setAttribute('muted', '');
    video.setAttribute('playsinline', '');
    video.setAttribute('disablepictureinpicture', '');
    video.setAttribute('disableremoteplayback', '');
    const img = this.doc.createElement('img');
    img.alt = '';
    img.decoding = 'async';
    el.append(video, img);
    this.stage.append(el);
    return { el, video, img, item: null, gen: 0, busyUntil: 0, hideTimer: null };
  }

  private createBuiltin(): HTMLDivElement {
    const el = this.doc.createElement('div');
    el.className = 'layer builtin';
    const brand = this.doc.createElement('div');
    brand.className = 'brand';
    const rd = this.doc.createElement('span');
    rd.textContent = 'RD';
    brand.append(rd, this.doc.createTextNode(' Mídia Indoor'));
    const tag = this.doc.createElement('div');
    tag.className = 'tagline';
    tag.textContent = 'Anuncie aqui';
    el.append(brand, tag);
    this.stage.append(el);
    return el;
  }

  private backLayer(): Layer {
    const [a, b] = this.layers;
    if (this.front === a) return b;
    if (this.front === b) return a;
    return a.busyUntil <= b.busyUntil ? a : b;
  }

  async prepare(item: RenderItem): Promise<PreparedHandle> {
    const layer = this.backLayer();
    const wait = layer.busyUntil - performance.now();
    if (wait > 0) await new Promise((r) => setTimeout(r, wait));
    if (layer.hideTimer) {
      clearTimeout(layer.hideTimer);
      layer.hideTimer = null;
    }
    layer.el.classList.remove('visible');
    this.unload(layer);
    const gen = ++layer.gen;
    layer.item = item;
    if (item.isVideo) {
      layer.img.classList.remove('on');
      layer.video.classList.add('on');
      layer.video.src = item.url;
      layer.video.load();
      await waitVideoReady(layer.video);
      if (layer.gen !== gen) throw new Error('camada reutilizada durante a pré-carga');
      try {
        layer.video.currentTime = 0;
      } catch {
        /* ignore */
      }
    } else {
      layer.video.classList.remove('on');
      layer.img.classList.add('on');
      layer.img.src = item.url;
      await layer.img.decode();
      if (layer.gen !== gen) throw new Error('camada reutilizada durante a pré-carga');
    }
    return new DomHandle(item, layer, gen);
  }

  builtin(): PreparedHandle {
    return new DomHandle(null, null, 0);
  }

  show(h: PreparedHandle, opts: { fadeMs: number; footerText: string | null }): void {
    const handle = h as DomHandle;
    const fade = Math.max(0, Math.min(5_000, opts.fadeMs | 0));
    this.stage.style.setProperty('--fade', `${fade}ms`);
    const prev = this.front;
    if (prev === (handle.layer ?? 'builtin')) {
      this.footer.setId(opts.footerText);
      return;
    }
    const el = handle.layer ? handle.layer.el : this.builtinEl;
    el.style.zIndex = String(++this.z);
    el.classList.add('visible');
    if (handle.layer && handle.item?.isVideo) {
      const p = handle.layer.video.play();
      if (p && typeof p.catch === 'function') {
        p.catch((e: unknown) => this.onPlaybackIssue?.(`play(): ${e instanceof Error ? `${e.name} ${e.message}` : String(e)}`));
      }
    }
    // A camada anterior só some depois do fade (fica por baixo até lá ⇒ sem quadro preto).
    if (prev === 'builtin') {
      const b = this.builtinEl;
      setTimeout(() => {
        if (this.front !== 'builtin') b.classList.remove('visible');
      }, fade);
    } else if (prev) {
      const layer = prev;
      layer.busyUntil = performance.now() + fade + 50;
      layer.hideTimer = setTimeout(() => {
        layer.hideTimer = null;
        if (this.front !== layer) {
          layer.el.classList.remove('visible');
          layer.video.pause();
        }
      }, fade + 20);
    }
    this.front = handle.layer ?? 'builtin';
    this.footer.setId(opts.footerText);
  }

  discard(h: PreparedHandle): void {
    const handle = h as DomHandle;
    const layer = handle.layer;
    if (!layer || this.front === layer || layer.gen !== handle.gen) return;
    layer.gen++;
    this.unload(layer);
  }

  onMediaEvent(h: PreparedHandle, cb: (ev: MediaEvent) => void): () => void {
    const handle = h as DomHandle;
    const layer = handle.layer;
    if (!layer) return () => undefined;
    const guard = (ev: MediaEvent) => (): void => {
      if (layer.gen === handle.gen) cb(ev);
    };
    const onEnded = guard('ended');
    const onError = guard('error');
    layer.video.addEventListener('ended', onEnded);
    layer.video.addEventListener('error', onError);
    layer.img.addEventListener('error', onError);
    if (handle.item?.isVideo && layer.video.ended) onEnded();
    return () => {
      layer.video.removeEventListener('ended', onEnded);
      layer.video.removeEventListener('error', onError);
      layer.img.removeEventListener('error', onError);
    };
  }

  /** Vídeo em exibição (para o watchdog). */
  frontVideo(): HTMLVideoElement | null {
    const f = this.front;
    if (!f || f === 'builtin' || !f.item?.isVideo) return null;
    return f.video;
  }

  reset(): void {
    for (const l of this.layers) {
      if (l.hideTimer) clearTimeout(l.hideTimer);
      this.unload(l);
      l.el.remove();
    }
    this.layers = [this.createLayer(), this.createLayer()];
    this.front = this.builtinEl.classList.contains('visible') ? 'builtin' : null;
  }

  clear(): void {
    for (const l of this.layers) {
      if (l.hideTimer) clearTimeout(l.hideTimer);
      l.el.classList.remove('visible');
      l.gen++;
      this.unload(l);
    }
    this.builtinEl.classList.remove('visible');
    this.front = null;
    this.footer.setId(null);
  }

  private unload(layer: Layer): void {
    const item = layer.item;
    layer.item = null;
    try {
      layer.video.pause();
    } catch {
      /* ignore */
    }
    if (layer.video.getAttribute('src')) {
      layer.video.removeAttribute('src');
      layer.video.load();
    }
    if (layer.img.getAttribute('src')) layer.img.removeAttribute('src');
    item?.release();
  }
}
