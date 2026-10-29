// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { computeAlerts } from '../src/core/alerts';
import { FooterView, footerHeightPx } from '../src/ui/footer';
import { DomRenderer } from '../src/ui/renderer';

// jsdom não implementa reprodução de mídia: neutraliza para não poluir a saída.
HTMLMediaElement.prototype.pause = () => undefined;
HTMLMediaElement.prototype.load = () => undefined;

function setup() {
  document.body.textContent = '';
  const root = document.createElement('div');
  root.id = 'app';
  document.body.append(root);
  const stage = document.createElement('div');
  stage.className = 'stage';
  root.append(stage);
  const footer = new FooterView(root, stage);
  const renderer = new DomRenderer(stage, footer);
  return { root, stage, footer, renderer };
}

describe('rodapé e área útil', () => {
  it('altura = height_permille/1000 da tela (60‰ de 1080 = 65 px)', () => {
    expect(footerHeightPx(1080, 60)).toBe(65);
    expect(footerHeightPx(2160, 60)).toBe(130);
    expect(footerHeightPx(1080, 0)).toBe(65); // padrão
  });

  it('texto "<label> — <code>" e a área útil termina acima do rodapé (não cobre a arte)', () => {
    const { root, stage, footer } = setup();
    footer.setLayout({ enabled: true, heightPermille: 60 });
    footer.setId('Publicidade — RD-A7M2P9');
    expect(footer.idText).toBe('Publicidade — RD-A7M2P9');
    expect(root.style.getPropertyValue('--footer-h')).toMatch(/px$/);
    expect(stage.classList.contains('no-footer')).toBe(false);
    expect(footer.el.classList.contains('hidden')).toBe(false);
  });

  it('rodapé desabilitado ⇒ área útil tela cheia; mas alerta força o rodapé a aparecer', () => {
    const { stage, footer } = setup();
    footer.setLayout({ enabled: false, heightPermille: 60 });
    expect(stage.classList.contains('no-footer')).toBe(true);
    const a = computeAlerts({ now: 10 * 86_400_000, lastSyncMs: 0 });
    footer.setAlert(a.footer);
    expect(stage.classList.contains('no-footer')).toBe(false);
    expect(footer.el.querySelector('.alert')!.textContent).toContain('SEM ATUALIZAÇÃO');
    footer.setAlert(null);
    expect(stage.classList.contains('no-footer')).toBe(true);
  });
});

describe('renderer double buffer', () => {
  it('duas camadas A/B + tela institucional; builtin fica visível e atualiza o rodapé', () => {
    const { stage, renderer, footer } = setup();
    expect(stage.querySelectorAll('.layer:not(.builtin)')).toHaveLength(2);
    expect(stage.querySelectorAll('.layer video[muted][playsinline]')).toHaveLength(2);
    renderer.show(renderer.builtin(), { fadeMs: 400, footerText: 'RD Mídia Indoor' });
    expect(stage.querySelector('.builtin')!.classList.contains('visible')).toBe(true);
    expect(footer.idText).toBe('RD Mídia Indoor');
    expect(stage.style.getPropertyValue('--fade')).toBe('400ms');
    renderer.clear();
    expect(stage.querySelector('.builtin')!.classList.contains('visible')).toBe(false);
  });
});
