import type { FieldErrors, UseFormRegister } from 'react-hook-form'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { PasswordInput } from './PasswordInput'

/** Nova senha + confirmação com a política do contrato v1 §2 visível. */
export function NewPasswordFields<T extends { new_password: string; confirm: string }>({
  register,
  errors,
  label = 'Nova senha',
}: {
  register: UseFormRegister<T>
  errors: FieldErrors<T>
  label?: string
}) {
  // Os nomes são fixos ('new_password' e 'confirm'); o cast evita genéricos verbosos do RHF.
  const reg = register as unknown as UseFormRegister<{ new_password: string; confirm: string }>
  const errs = errors as FieldErrors<{ new_password: string; confirm: string }>
  return (
    <>
      <Field invalid={!!errs.new_password}>
        <FieldLabel required>{label}</FieldLabel>
        <PasswordInput autoComplete="new-password" {...reg('new_password')} />
        <FieldDescription>Mínimo de 10 caracteres, com letras e números, sem conter seu e-mail.</FieldDescription>
        <FieldError>{errs.new_password?.message}</FieldError>
      </Field>
      <Field invalid={!!errs.confirm}>
        <FieldLabel required>Confirme a nova senha</FieldLabel>
        <PasswordInput autoComplete="new-password" {...reg('confirm')} />
        <FieldError>{errs.confirm?.message}</FieldError>
      </Field>
    </>
  )
}
