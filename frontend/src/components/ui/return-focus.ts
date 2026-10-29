import * as React from 'react'

type AutoFocusHandler = (event: Event) => void

/**
 * Devolve o foco a quem abriu o overlay (Dialog, AlertDialog/ConfirmDialog, Sheet) — inclusive quando
 * ele é aberto por estado (sem `*Trigger`), caso em que o Radix deixaria o foco cair no `<body>`.
 * Guarda `document.activeElement` no `onOpenAutoFocus` (ainda é o gatilho; o Radix move o foco depois)
 * e o restaura no `onCloseAutoFocus`, COMPONDO os handlers do chamador: se ele chamar
 * `event.preventDefault()` (assumiu o foco) ou o gatilho saiu do DOM, vale o comportamento padrão.
 */
export function useReturnFocus(onOpenAutoFocus?: AutoFocusHandler, onCloseAutoFocus?: AutoFocusHandler) {
  const returnFocusRef = React.useRef<HTMLElement | null>(null)
  return {
    onOpenAutoFocus: (event: Event) => {
      const active = document.activeElement
      returnFocusRef.current = active instanceof HTMLElement && active !== document.body ? active : null
      onOpenAutoFocus?.(event)
    },
    onCloseAutoFocus: (event: Event) => {
      onCloseAutoFocus?.(event)
      const target = returnFocusRef.current
      returnFocusRef.current = null
      if (event.defaultPrevented || !target?.isConnected) return
      event.preventDefault()
      target.focus()
    },
  }
}
