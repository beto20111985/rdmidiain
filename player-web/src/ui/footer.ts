/**
 * Rodapé (RDPP §5.1.10; ETAPA2 #359–#365; P001 §36): tarja preta, texto branco "<label> — <code>" no canto
 * inferior direito, altura = height_permille/1000 da tela. A área útil do anúncio é reduzida (o rodapé nunca
 * cobre a arte). O mesmo rodapé mostra os alertas (7 dias offline, relógio, armazenamento).
 */

import type { AlertView } from '../core/alerts';

export interface FooterLayout {
  enabled: boolean;
  heightPermille: number;
}

export function footerHeightPx(viewportHeight: number, heightPermille: number): number {
  const permille = Number.isFinite(heightPermille) && heightPermille > 0 ? heightPermille : 60;
  return Math.max(16, Math.round((viewportHeight * Math.min(permille, 200)) / 1000));
}

export class FooterView {
  readonly el: HTMLDivElement;
  private readonly alertEl: HTMLSpanElement;
  private readonly idEl: HTMLSpanElement;
  private layout: FooterLayout = { enabled: true, heightPermille: 60 };
  private alert: AlertView | null = null;

  constructor(
    private readonly root: HTMLElement,
    private readonly stage: HTMLElement,
    doc: Document = document,
    /** Altura LÓGICA da tela (com rotação por software, é a largura física). */
    private readonly screenHeight: () => number = () => root.clientHeight || globalThis.innerHeight || 1080,
  ) {
    this.el = doc.createElement('div');
    this.el.className = 'footer';
    this.el.setAttribute('role', 'contentinfo');
    this.alertEl = doc.createElement('span');
    this.alertEl.className = 'alert';
    this.alertEl.setAttribute('role', 'status');
    this.idEl = doc.createElement('span');
    this.idEl.className = 'id';
    this.el.append(this.alertEl, this.idEl);
    root.append(this.el);
    this.apply();
  }

  setLayout(layout: FooterLayout): void {
    this.layout = layout;
    this.apply();
  }

  /** Recalcula a altura (ex.: resize/rotação). */
  relayout(): void {
    this.apply();
  }

  setId(text: string | null): void {
    this.idEl.textContent = text ?? '';
  }

  get idText(): string {
    return this.idEl.textContent ?? '';
  }

  setAlert(view: AlertView | null): void {
    this.alert = view;
    this.alertEl.textContent = view?.text ?? '';
    this.alertEl.classList.toggle('on', view !== null);
    this.alertEl.classList.toggle('danger', view?.code === 'CLOCK_INVALID' || view?.code === 'DISK_FULL');
    this.apply();
  }

  private apply(): void {
    const visible = this.layout.enabled || this.alert !== null;
    const h = footerHeightPx(this.screenHeight() || 1080, this.layout.heightPermille);
    this.root.style.setProperty('--footer-h', `${h}px`);
    this.el.classList.toggle('hidden', !visible);
    this.stage.classList.toggle('no-footer', !visible);
  }
}
