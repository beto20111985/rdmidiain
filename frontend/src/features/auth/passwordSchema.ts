import { z } from 'zod'
import { passwordProblems } from './api'

/** Nova senha + confirmação (política do contrato v1 §2). `emailField` = campo do formulário com o e-mail. */
export function newPasswordSchema<S extends z.ZodRawShape>(extra: S, emailOf?: (v: Record<string, unknown>) => string | undefined) {
  return z
    .object({
      ...extra,
      new_password: z.string(),
      confirm: z.string(),
    })
    .superRefine((v, ctx) => {
      const values = v as Record<string, unknown> & { new_password: string; confirm: string }
      const problems = passwordProblems(values.new_password, emailOf?.(values))
      if (problems.length) ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['new_password'], message: problems.join(' ') })
      if (values.confirm !== values.new_password)
        ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['confirm'], message: 'As senhas não coincidem.' })
    })
}
