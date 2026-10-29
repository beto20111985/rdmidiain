import { type ClassValue, clsx } from 'clsx'
import { extendTailwindMerge } from 'tailwind-merge'

/**
 * `cn()` com tailwind-merge ciente da escala tipográfica do DS — sem isto,
 * `text-title-page` e `text-text-muted` seriam tratados como conflito (ambos "text-*").
 */
const twMergeRd = extendTailwindMerge({
  extend: {
    classGroups: {
      'font-size': [
        {
          text: [
            'caption',
            'badge',
            'body',
            'title-card',
            'title-section',
            'title-page-sm',
            'title-page',
            'kpi-secondary',
            'kpi-sm',
            'kpi',
          ],
        },
      ],
    },
  },
})

export function cn(...inputs: ClassValue[]) {
  return twMergeRd(clsx(inputs))
}

export function getInitials(name: string | null | undefined): string {
  if (!name) return '?'
  return (
    name
      .split(/\s+/)
      .filter(Boolean)
      .slice(0, 2)
      .map((w) => w[0]?.toUpperCase() ?? '')
      .join('') || '?'
  )
}
