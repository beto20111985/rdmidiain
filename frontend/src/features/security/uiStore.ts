/**
 * Estado de UI do aviso do 2º Master — SOMENTE em memória (nada em localStorage/sessionStorage).
 * "Fazer depois" no modal vale até o fim da sessão: cada login/boot mostra o modal de novo (SPEC §11.3);
 * o banner persistente continua até o 2º Master concluir. Zerado em `clearLocal` do authStore.
 */
import { create } from 'zustand'

interface SecurityUiState {
  secondMasterModalDismissed: boolean
  dismissSecondMasterModal: () => void
  reset: () => void
}

export const useSecurityUiStore = create<SecurityUiState>((set) => ({
  secondMasterModalDismissed: false,
  dismissSecondMasterModal: () => set({ secondMasterModalDismissed: true }),
  reset: () => set({ secondMasterModalDismissed: false }),
}))
