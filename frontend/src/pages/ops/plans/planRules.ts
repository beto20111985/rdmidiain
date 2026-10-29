/** Regra da rotação padrão do plano (espelha contracts._validate_plan do backend). */
export function rotationValid(i: number, e: number): boolean {
  return i >= 0 && e >= 30 && i % 30 === 0 && e % 30 === 0 && 3600 % (i + e) === 0
}
