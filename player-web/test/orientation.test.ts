// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';
import { screenOrientation } from '../src/adapters/web/platform';
import { FooterView } from '../src/ui/footer';
import { computeStageGeometry, physicalOrientation, ScreenOrienter } from '../src/ui/orientation';
import { DomRenderer } from '../src/ui/renderer';

HTMLMediaElement.prototype.pause = () => undefined;
HTMLMediaElement.prototype.load = () => undefined;

describe('cálculo de rotação/dimensões (orientação configurada × física)', () => {
  it('mesma orientação, sem configuração ou tela quadrada ⇒ não gira', () => {
    expect(computeStageGeometry('LANDSCAPE', 1920, 1080)).toEqual({ rotate: 0, width: 1920, height: 1080, left: 0, top: 0 });
    expect(computeStageGeometry('PORTRAIT', 1080, 1920)).toEqual({ rotate: 0, width: 1080, height: 1920, left: 0, top: 0 });
    expect(computeStageGeometry(null, 1920, 1080).rotate).toBe(0);
    expect(computeStageGeometry('PORTRAIT', 1000, 1000).rotate).toBe(0);
  });

  it('RETRATO configurado em saída PAISAGEM (TV de pé em HDMI 1920×1080) ⇒ +90°, tela lógica 1080×1920 centralizada', () => {
    const g = computeStageGeometry('PORTRAIT', 1920, 1080);
    expect(g).toEqual({ rotate: 90, width: 1080, height: 1920, left: 420, top: -420 });
    // girada em torno do centro, a caixa cobre exatamente a viewport: centro = centro da viewport
    expect(g.left + g.width / 2).toBe(1920 / 2);
    expect(g.top + g.height / 2).toBe(1080 / 2);
  });

  it('PAISAGEM configurada em saída RETRATO ⇒ −90°, tela lógica 1920×1080', () => {
    expect(computeStageGeometry('LANDSCAPE', 1080, 1920)).toEqual({ rotate: -90, width: 1920, height: 1080, left: -420, top: 420 });
  });

  it('orientação física (telemetria) depende só da viewport', () => {
    expect(physicalOrientation(1920, 1080)).toBe('LANDSCAPE');
    expect(physicalOrientation(1080, 1920)).toBe('PORTRAIT');
  });
});

describe('aplicação no contêiner .screen (jsdom)', () => {
  function setup(vw: number, vh: number) {
    document.body.textContent = '';
    const app = document.createElement('div');
    document.body.append(app);
    const screen = document.createElement('div');
    screen.className = 'screen';
    app.append(screen);
    const viewport = { innerWidth: vw, innerHeight: vh };
    const orienter = new ScreenOrienter(screen, viewport);
    orienter.relayout();
    const stage = document.createElement('div');
    stage.className = 'stage';
    screen.append(stage);
    const footer = new FooterView(screen, stage, document, () => orienter.logicalHeight);
    const renderer = new DomRenderer(stage, footer);
    return { screen, stage, footer, renderer, orienter, viewport };
  }

  it('gira a tela inteira (camadas A/B, institucional e rodapé ficam DENTRO do contêiner girado)', () => {
    const { screen, stage, footer, orienter } = setup(1920, 1080);
    expect(orienter.setConfigured('PORTRAIT')).toBe(true);
    expect(screen.style.transform).toBe('rotate(90deg)');
    expect(screen.style.width).toBe('1080px');
    expect(screen.style.height).toBe('1920px');
    expect(screen.style.left).toBe('420px');
    expect(screen.style.top).toBe('-420px');
    expect(screen.style.getPropertyValue('--vh')).toBe('19.2px');
    expect(screen.style.getPropertyValue('--vw')).toBe('10.8px');
    expect(screen.dataset.rotate).toBe('90');
    expect(screen.contains(stage)).toBe(true);
    expect(screen.contains(footer.el)).toBe(true);
    expect(stage.querySelectorAll('.layer')).toHaveLength(3); // A, B e institucional
  });

  it('rodapé usa a altura LÓGICA: 60‰ de 1920 = 115 px na TV de pé (não 65 px da saída física)', () => {
    const { screen, footer, orienter } = setup(1920, 1080);
    orienter.setConfigured('PORTRAIT');
    footer.setLayout({ enabled: true, heightPermille: 60 });
    expect(screen.style.getPropertyValue('--footer-h')).toBe('115px');
    orienter.setConfigured('LANDSCAPE');
    footer.relayout();
    expect(screen.style.transform).toBe('none');
    expect(screen.style.getPropertyValue('--footer-h')).toBe('65px');
  });

  it('sem reflow por item: geometria igual não reescreve estilos; troca de item não toca o contêiner', () => {
    const { screen, renderer, footer, orienter } = setup(1920, 1080);
    orienter.setConfigured('PORTRAIT');
    let writes = 0;
    const obs = new MutationObserver((ms) => (writes += ms.filter((m) => m.target === screen && m.attributeName === 'style').length));
    obs.observe(screen, { attributes: true });
    expect(orienter.setConfigured('PORTRAIT')).toBe(false);
    expect(orienter.relayout()).toBe(false);
    renderer.show(renderer.builtin(), { fadeMs: 400, footerText: 'RD Mídia Indoor' });
    footer.setId('Publicidade — RD-A7M2P9');
    obs.takeRecords().forEach(() => writes++);
    obs.disconnect();
    expect(writes).toBe(0);
  });

  it('resize/troca de saída recalcula; volta para paisagem física ⇒ sem rotação', () => {
    const { screen, orienter, viewport } = setup(1920, 1080);
    orienter.setConfigured('PORTRAIT');
    viewport.innerWidth = 1080;
    viewport.innerHeight = 1920;
    expect(orienter.relayout()).toBe(true);
    expect(screen.style.transform).toBe('none');
    expect(orienter.logicalHeight).toBe(1920);
  });

  it('telemetria continua reportando a orientação FÍSICA mesmo com a tela girada', () => {
    const { orienter } = setup(1920, 1080);
    Object.defineProperty(window, 'innerWidth', { value: 1920, configurable: true });
    Object.defineProperty(window, 'innerHeight', { value: 1080, configurable: true });
    orienter.setConfigured('PORTRAIT');
    expect(screenOrientation()).toBe('LANDSCAPE');
  });
});
