import type { Config } from 'tailwindcss'
import animate from 'tailwindcss-animate'

/**
 * RD Mídia Indoor — Tailwind mapeado 100% para os tokens CSS de `src/styles/globals.css`.
 * Nenhuma cor em hexadecimal aqui: os dois temas (escuro = padrão, claro = [data-theme="light"])
 * trocam só as variáveis. Cores guardadas como canais RGB para permitir `bg-action/90` etc.
 */
const token = (name: string) => `rgb(var(--${name}) / <alpha-value>)`

const config: Config = {
  darkMode: ['selector', '[data-theme="dark"]'],
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    // Breakpoints do DS §8: <768 gaveta · 768–1199 compacta · ≥1200 completa
    screens: {
      sm: '480px',
      md: '768px',
      lg: '1024px',
      xl: '1200px',
      '2xl': '1440px',
    },
    extend: {
      colors: {
        // Tokens RD
        bg: token('bg'),
        sidebar: token('sidebar'),
        surface: token('surface'),
        'surface-raised': token('surface-raised'),
        'surface-hover': token('surface-hover'),
        field: token('field'),
        border: token('border'),
        'control-border': token('control-border'),
        text: token('text'),
        'text-muted': token('text-muted'),
        action: {
          DEFAULT: token('action'),
          hover: token('action-hover'),
          foreground: token('action-foreground'),
        },
        link: token('link'),
        focus: token('focus'),
        success: { DEFAULT: token('success'), soft: 'rgb(var(--success) / var(--badge-alpha))' },
        warning: { DEFAULT: token('warning'), soft: 'rgb(var(--warning) / var(--badge-alpha))' },
        danger: { DEFAULT: token('danger'), soft: 'rgb(var(--danger) / var(--badge-alpha))' },
        info: { DEFAULT: token('info'), soft: 'rgb(var(--info) / var(--badge-alpha))' },
        neutral: { DEFAULT: token('neutral'), soft: 'rgb(var(--neutral) / var(--badge-alpha))' },
        chart: {
          1: token('chart-1'),
          2: token('chart-2'),
          3: token('chart-3'),
          4: token('chart-4'),
          track: token('chart-track'),
          grid: token('chart-grid'),
        },
        brand: {
          black: 'var(--brand-black)',
          graphite: 'var(--brand-graphite)',
          'deep-blue': 'var(--brand-deep-blue)',
          blue: 'var(--brand-blue)',
          'light-blue': 'var(--brand-light-blue)',
          white: 'var(--brand-white)',
          gray: 'var(--brand-gray)',
        },
        // Aliases semânticos esperados pelos componentes shadcn/ui
        background: token('bg'),
        foreground: token('text'),
        card: { DEFAULT: token('surface'), foreground: token('text') },
        popover: { DEFAULT: token('surface-raised'), foreground: token('text') },
        primary: { DEFAULT: token('action'), foreground: token('action-foreground') },
        secondary: { DEFAULT: token('surface-hover'), foreground: token('text') },
        muted: { DEFAULT: token('surface-hover'), foreground: token('text-muted') },
        accent: { DEFAULT: token('surface-hover'), foreground: token('text') },
        destructive: { DEFAULT: token('danger'), foreground: token('text') },
        input: token('control-border'),
        ring: token('focus'),
      },
      fontFamily: {
        sans: ['Manrope', 'Inter', 'system-ui', 'sans-serif'],
        ui: ['Manrope', 'Inter', 'system-ui', 'sans-serif'],
      },
      // Escala tipográfica do DS §7 (tamanho/entrelinha/peso)
      fontSize: {
        xs: ['12px', { lineHeight: '18px' }],
        sm: ['14px', { lineHeight: '22px' }],
        base: ['16px', { lineHeight: '24px' }],
        caption: ['12px', { lineHeight: '18px', fontWeight: '500' }],
        badge: ['12px', { lineHeight: '16px', fontWeight: '600' }],
        body: ['14px', { lineHeight: '22px' }],
        'title-card': ['14px', { lineHeight: '20px', fontWeight: '600' }],
        'title-section': ['20px', { lineHeight: '28px', fontWeight: '700' }],
        'title-page-sm': ['24px', { lineHeight: '32px', fontWeight: '700' }],
        'title-page': ['28px', { lineHeight: '36px', fontWeight: '700' }],
        'kpi-secondary': ['28px', { lineHeight: '36px', fontWeight: '700' }],
        'kpi-sm': ['32px', { lineHeight: '40px', fontWeight: '700' }],
        kpi: ['40px', { lineHeight: '48px', fontWeight: '700' }],
      },
      letterSpacing: {
        metric: '0.04em',
      },
      // Raios do DS §8: 8 controles · 12 internos · 16 cartões · 20 modais
      borderRadius: {
        sm: '6px',
        md: '8px',
        lg: '12px',
        xl: '16px',
        '2xl': '20px',
        control: 'var(--radius-control)',
        inner: 'var(--radius-inner)',
        card: 'var(--radius-card)',
        modal: 'var(--radius-modal)',
      },
      spacing: {
        sidebar: 'var(--sidebar-width)',
        'sidebar-collapsed': 'var(--sidebar-collapsed)',
        header: 'var(--header-height)',
        control: '44px',
      },
      minHeight: {
        control: '44px',
        header: 'var(--header-height)',
        row: '52px',
      },
      minWidth: {
        control: '44px',
      },
      maxWidth: {
        content: 'var(--content-max)',
        'modal-sm': '480px',
        'modal-md': '720px',
        drawer: '480px',
      },
      boxShadow: {
        card: 'var(--shadow-card)',
        overlay: 'var(--shadow-overlay)',
      },
      zIndex: {
        header: 'var(--z-header)',
        dropdown: 'var(--z-dropdown)',
        overlay: 'var(--z-overlay)',
        modal: 'var(--z-modal)',
        popover: 'var(--z-popover)',
        toast: 'var(--z-toast)',
        banner: 'var(--z-banner)',
      },
      transitionDuration: {
        fast: '160ms',
        normal: '220ms',
      },
      transitionTimingFunction: {
        rd: 'cubic-bezier(.2, 0, 0, 1)',
      },
      gridTemplateColumns: {
        dashboard: 'repeat(12, minmax(0, 1fr))',
      },
    },
  },
  plugins: [animate],
}

export default config
