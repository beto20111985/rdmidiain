/**
 * Orientação configurada da tela (RDPP §5 `settings.orientation`: LANDSCAPE | PORTRAIT; no backend
 * `screens.orientation` HORIZONTAL/VERTICAL) × orientação FÍSICA da saída (viewport).
 *
 * Quando diferem, a tela inteira do player (camadas A/B, tela institucional, rodapé/alertas e overlays) é girada por
 * software num único contêiner `.screen` com largura/altura trocadas — mesmo comportamento do Android
 * (`RotatableContainer`: retrato configurado numa saída paisagem gira +90°). Paisagem configurada numa saída retrato
 * gira −90°. O contêiner só é tocado quando a geometria muda (troca de manifesto na fronteira de ciclo, resize/HDMI):
 * nenhum reflow por item; o double buffer fica intacto dentro dele. A telemetria continua reportando a orientação
 * física (`platform.ts`).
 */

import type { Orientation } from '../core/protocol';

export type Rotation = 0 | 90 | -90;

export interface StageGeometry {
  rotate: Rotation;
  /** dimensões LÓGICAS da tela (após a rotação, como o conteúdo as enxerga) */
  width: number;
  height: number;
  /** posição do contêiner (antes do transform) para que, girado em torno do centro, cubra a viewport */
  left: number;
  top: number;
}

export function physicalOrientation(viewportW: number, viewportH: number): Orientation {
  return viewportW >= viewportH ? 'LANDSCAPE' : 'PORTRAIT';
}

/** Cálculo puro da rotação/dimensões. Sem orientação configurada (ou tela quadrada) não gira. */
export function computeStageGeometry(configured: Orientation | null | undefined, viewportW: number, viewportH: number): StageGeometry {
  const W = Math.max(0, Math.round(viewportW));
  const H = Math.max(0, Math.round(viewportH));
  const physical = physicalOrientation(W, H);
  if (!configured || W === H || configured === physical) return { rotate: 0, width: W, height: H, left: 0, top: 0 };
  // Caixa com largura/altura trocadas, centralizada; girada ±90° em torno do centro cobre exatamente a viewport.
  return {
    rotate: configured === 'PORTRAIT' ? 90 : -90,
    width: H,
    height: W,
    left: (W - H) / 2,
    top: (H - W) / 2,
  };
}

export interface ViewportSource {
  innerWidth: number;
  innerHeight: number;
}

/** Aplica a geometria ao contêiner `.screen` (só quando muda). */
export class ScreenOrienter {
  private configured: Orientation | null = null;
  private current: StageGeometry | null = null;
  private listeners = new Set<(g: StageGeometry) => void>();

  constructor(
    private readonly el: HTMLElement,
    private readonly viewport: ViewportSource = globalThis as unknown as ViewportSource,
  ) {}

  get geometry(): StageGeometry {
    return this.current ?? computeStageGeometry(this.configured, this.viewport.innerWidth, this.viewport.innerHeight);
  }

  /** Altura lógica (base do rodapé `height_permille`). */
  get logicalHeight(): number {
    return this.geometry.height;
  }

  get logicalWidth(): number {
    return this.geometry.width;
  }

  onChange(fn: (g: StageGeometry) => void): () => void {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  /** Nova orientação configurada (manifesto ativado). Devolve true se a geometria mudou. */
  setConfigured(o: Orientation | null | undefined): boolean {
    this.configured = o === 'LANDSCAPE' || o === 'PORTRAIT' ? o : null;
    return this.relayout();
  }

  /** Recalcula (resize, troca de saída de vídeo). Devolve true se a geometria mudou. */
  relayout(): boolean {
    const g = computeStageGeometry(this.configured, this.viewport.innerWidth, this.viewport.innerHeight);
    const c = this.current;
    if (c && c.rotate === g.rotate && c.width === g.width && c.height === g.height && c.left === g.left && c.top === g.top) {
      return false;
    }
    this.current = g;
    const s = this.el.style;
    s.width = `${g.width}px`;
    s.height = `${g.height}px`;
    s.left = `${g.left}px`;
    s.top = `${g.top}px`;
    s.transform = g.rotate === 0 ? 'none' : `rotate(${g.rotate}deg)`;
    // Unidades relativas à tela LÓGICA (substituem vh/vw no CSS — que seguiriam a viewport física).
    s.setProperty('--vh', `${g.height / 100}px`);
    s.setProperty('--vw', `${g.width / 100}px`);
    this.el.dataset.rotate = String(g.rotate);
    for (const fn of this.listeners) fn(g);
    return true;
  }
}
