/**
 * Overlays: pareamento (código grande + QR local), alerta central de 28 dias, mensagens (revogado, sem rede,
 * contexto inseguro) e o formulário opcional de login controlado do técnico (#tecnico).
 * Sem innerHTML (Trusted Types): tudo via createElement/textContent. QR gerado localmente (biblioteca `qrcode`).
 */

import QRCode from 'qrcode';
import type { AlertView } from '../core/alerts';

export interface TechClaim {
  login: string;
  password: string;
  totp: string;
  screen_code: string;
}

function el<K extends keyof HTMLElementTagNameMap>(doc: Document, tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const e = doc.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

export class Overlays {
  private readonly pairing: HTMLDivElement;
  private readonly codeEl: HTMLDivElement;
  private readonly qr: HTMLCanvasElement;
  private readonly pairMeta: HTMLParagraphElement;
  private readonly pairStatus: HTMLParagraphElement;
  private readonly tech: HTMLFormElement;
  private readonly techMsg: HTMLDivElement;
  private readonly alert28: HTMLDivElement;
  private readonly alert28Text: HTMLParagraphElement;
  private readonly message: HTMLDivElement;
  private readonly msgTitle: HTMLHeadingElement;
  private readonly msgText: HTMLParagraphElement;
  private techHandler: ((c: TechClaim) => Promise<void>) | null = null;

  constructor(root: HTMLElement, doc: Document = document) {
    // Pareamento
    this.pairing = el(doc, 'div', 'overlay full');
    const card = el(doc, 'div', 'card');
    const title = el(doc, 'h1', undefined, 'Parear esta tela');
    const row = el(doc, 'div', 'pairing');
    const left = el(doc, 'div');
    left.append(
      el(doc, 'p', undefined, 'No painel RD Mídia Indoor, abra Telas → Parear e informe o código:'),
      (this.codeEl = el(doc, 'div', 'code', '····-····')),
      (this.pairMeta = el(doc, 'p', 'meta', '')),
      (this.pairStatus = el(doc, 'p', 'meta', '')),
    );
    this.qr = el(doc, 'canvas');
    this.qr.width = 512;
    this.qr.height = 512;
    this.qr.setAttribute('aria-label', 'QR Code de pareamento');
    row.append(left, this.qr);
    // Login controlado do técnico (credenciais só em memória, nunca armazenadas)
    this.tech = el(doc, 'form', 'tech');
    this.tech.autocomplete = 'off';
    const inputs: [string, string, string][] = [
      ['login', 'text', 'Login do técnico'],
      ['password', 'password', 'Senha'],
      ['totp', 'text', 'Código 2FA (se houver)'],
      ['screen_code', 'text', 'Código da tela (TEL-UF-XXXX)'],
    ];
    for (const [name, type, ph] of inputs) {
      const i = el(doc, 'input');
      i.name = name;
      i.type = type;
      i.placeholder = ph;
      i.autocomplete = 'off';
      i.spellcheck = false;
      this.tech.append(i);
    }
    const btn = el(doc, 'button', undefined, 'Parear com login do técnico');
    btn.type = 'submit';
    this.techMsg = el(doc, 'div', 'msg');
    this.tech.append(btn, this.techMsg);
    this.tech.addEventListener('submit', (ev) => {
      ev.preventDefault();
      void this.submitTech();
    });
    card.append(title, row, this.tech);
    this.pairing.append(card);

    // Alerta central (28 dias)
    this.alert28 = el(doc, 'div', 'overlay alert28');
    const c28 = el(doc, 'div', 'card');
    c28.append(el(doc, 'h1', undefined, 'EQUIPAMENTO SEM ATUALIZAÇÃO'), (this.alert28Text = el(doc, 'p')));
    this.alert28.append(c28);

    // Mensagens genéricas
    this.message = el(doc, 'div', 'overlay full');
    const cm = el(doc, 'div', 'card');
    cm.append((this.msgTitle = el(doc, 'h1')), (this.msgText = el(doc, 'p')));
    this.message.append(cm);

    root.append(this.alert28, this.message, this.pairing);
  }

  async showPairing(p: { code: string; qrUrl: string | null; expiresAtMs: number | null; now: number }): Promise<void> {
    this.hideMessage();
    this.codeEl.textContent = p.code;
    this.pairMeta.textContent = p.expiresAtMs ? `Código válido por ${Math.max(0, Math.round((p.expiresAtMs - p.now) / 60_000))} min` : '';
    this.pairStatus.textContent = '';
    this.pairing.classList.add('on');
    const ctx = this.qr.getContext('2d');
    if (p.qrUrl) {
      await QRCode.toCanvas(this.qr, p.qrUrl, { width: 512, margin: 1, errorCorrectionLevel: 'M', color: { dark: '#000000', light: '#ffffff' } });
      // A biblioteca fixa width/height inline; o tamanho visível é do CSS (responsivo).
      this.qr.style.removeProperty('width');
      this.qr.style.removeProperty('height');
    } else {
      ctx?.clearRect(0, 0, this.qr.width, this.qr.height);
    }
  }

  setPairingStatus(text: string): void {
    this.pairStatus.textContent = text;
  }

  hidePairing(): void {
    this.pairing.classList.remove('on');
    this.tech.classList.remove('on');
  }

  get pairingVisible(): boolean {
    return this.pairing.classList.contains('on');
  }

  enableTech(handler: (c: TechClaim) => Promise<void>, visible: boolean): void {
    this.techHandler = handler;
    this.tech.classList.toggle('on', visible);
  }

  showTech(visible: boolean): void {
    this.tech.classList.toggle('on', visible);
  }

  private async submitTech(): Promise<void> {
    if (!this.techHandler) return;
    const f = new FormData(this.tech);
    const claim: TechClaim = {
      login: String(f.get('login') ?? '').trim(),
      password: String(f.get('password') ?? ''),
      totp: String(f.get('totp') ?? '').trim(),
      screen_code: String(f.get('screen_code') ?? '').trim().toUpperCase(),
    };
    this.techMsg.textContent = 'Enviando…';
    try {
      await this.techHandler(claim);
      this.techMsg.textContent = 'Pareamento solicitado.';
    } catch (e) {
      this.techMsg.textContent = e instanceof Error ? e.message : String(e);
    } finally {
      // credenciais não ficam na tela/memória do formulário
      (this.tech.elements.namedItem('password') as HTMLInputElement | null)?.setAttribute('value', '');
      const pw = this.tech.elements.namedItem('password') as HTMLInputElement | null;
      if (pw) pw.value = '';
      const totp = this.tech.elements.namedItem('totp') as HTMLInputElement | null;
      if (totp) totp.value = '';
    }
  }

  showAlert28(view: AlertView | null): void {
    this.alert28Text.textContent = view?.text ?? '';
    this.alert28.classList.toggle('on', view !== null);
  }

  showMessage(title: string, text: string): void {
    this.msgTitle.textContent = title;
    this.msgText.textContent = text;
    this.message.classList.add('on');
  }

  hideMessage(): void {
    this.message.classList.remove('on');
  }
}
