/** Tons de badge da manutenção (status e prioridade). */
export const maintenanceTone = (s: string): 'success' | 'warning' | 'danger' | 'neutral' | 'info' =>
  s === 'RESOLVED' ? 'success' : s === 'CANCELLED' ? 'neutral' : s === 'WAITING_PARTS' ? 'warning' : s === 'OPEN' ? 'danger' : 'info'

export const priorityTone = (p: string): 'success' | 'warning' | 'danger' | 'neutral' | 'info' =>
  p === 'URGENT' ? 'danger' : p === 'HIGH' ? 'warning' : p === 'LOW' ? 'neutral' : 'info'
