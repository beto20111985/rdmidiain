/**
 * Perfil + escopo (GLOBAL / ORGANIZATION / ESTABLISHMENT / CITY) para convites e
 * atribuições. As opções seguem os escopos típicos de cada perfil (GET /roles) e o que o
 * ator parece poder conceder; a decisão final é do servidor (403 ROLE_ESCALATION,
 * 404 fora do escopo) — a tela mostra o motivo.
 */
import * as React from 'react'
import { Field, FieldDescription, FieldError, FieldLabel } from '@/components/ui/field'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { EstablishmentPicker, OrgPicker } from '@/components/admin/pickers'
import { ROLE_LABELS, SCOPE_LABELS, UFS, type ScopeType } from '@/components/admin/labels'
import { useRoles, type RoleInfo } from '@/components/admin/directory'
import { useAuthStore } from '@/stores/authStore'
import type { RoleScopeValue } from './roleScope'

function actorScopes(assignments: Array<{ scope_type: string }> | undefined): Set<ScopeType> {
  const kinds = new Set((assignments ?? []).map((a) => a.scope_type))
  const out = new Set<ScopeType>(['ORGANIZATION', 'ESTABLISHMENT'])
  if (kinds.has('GLOBAL')) {
    out.add('GLOBAL')
    out.add('CITY')
  }
  if (kinds.has('CITY')) out.add('CITY')
  return out
}

export function RoleScopeFields({
  value,
  onChange,
  problem,
}: {
  value: RoleScopeValue
  onChange: (v: RoleScopeValue) => void
  problem?: string | null
}) {
  const roles = useRoles()
  const me = useAuthStore((s) => s.me)
  const myPerms = React.useMemo(() => new Set(me?.permissions ?? []), [me?.permissions])
  const allowedByActor = actorScopes(me?.assignments)
  const role: RoleInfo | undefined = roles.data?.find((r) => r.slug === value.role)
  const typical = (role?.typical_scopes?.length ? role.typical_scopes : ['GLOBAL', 'ORGANIZATION', 'ESTABLISHMENT', 'CITY']) as ScopeType[]
  const scopeOptions = typical.filter((s) => allowedByActor.has(s))
  const exceeds = role ? role.permissions.filter((p) => !myPerms.has(p)) : []
  const scopeLabelId = React.useId()

  React.useEffect(() => {
    if (role && scopeOptions.length && !scopeOptions.includes(value.scope_type)) {
      onChange({ ...value, scope_type: scopeOptions[0], scope: null })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [role?.slug])

  return (
    <div className="flex flex-col gap-5">
      <Field required invalid={!value.role && !!problem}>
        <FieldLabel>Perfil</FieldLabel>
        <Select value={value.role} onValueChange={(r) => onChange({ ...value, role: r, scope: null })}>
          <SelectTrigger>
            <SelectValue placeholder={roles.isLoading ? 'Carregando perfis…' : 'Selecione o perfil'} />
          </SelectTrigger>
          <SelectContent>
            <SelectGroup>
              {/* Super Admin é reservado aos Administradores Master (AÇÃO 003): o servidor responde 403 ROLE_RESERVED_FOR_ADMIN_MASTER. */}
              {(roles.data ?? []).filter((r) => r.slug !== 'super_admin').map((r) => (
                <SelectItem key={r.id} value={r.slug}>
                  {r.name || ROLE_LABELS[r.slug] || r.slug}
                  {r.is_system ? '' : ' (personalizado)'}
                </SelectItem>
              ))}
            </SelectGroup>
          </SelectContent>
        </Select>
        {role ? (
          <FieldDescription>
            {role.description ?? ''}
            {role.mfa_required ? ' Exige verificação em duas etapas.' : ''}
            {exceeds.length
              ? ` Atenção: o perfil inclui ${exceeds.length} permissão(ões) que você não tem — o servidor pode recusar (escalonamento).`
              : ''}
          </FieldDescription>
        ) : null}
      </Field>

      {value.role ? (
        <Field>
          <FieldLabel id={scopeLabelId}>Escopo</FieldLabel>
          {scopeOptions.length ? (
            <ToggleGroup
              type="single"
              aria-labelledby={scopeLabelId}
              value={value.scope_type}
              onValueChange={(s) => s && onChange({ ...value, scope_type: s as ScopeType, scope: null })}
              className="w-fit"
            >
              {scopeOptions.map((s) => (
                <ToggleGroupItem key={s} value={s}>
                  {SCOPE_LABELS[s]}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          ) : (
            <p className="text-sm text-text-muted">Seu acesso não permite conceder este perfil em nenhum escopo.</p>
          )}
          <FieldDescription>
            {value.scope_type === 'GLOBAL'
              ? 'Acesso em toda a plataforma.'
              : value.scope_type === 'CITY'
                ? 'Acesso aos registros de uma cidade (UF + cidade).'
                : value.scope_type === 'ORGANIZATION'
                  ? 'Acesso aos dados de uma empresa (e seus estabelecimentos).'
                  : 'Acesso a um estabelecimento.'}
          </FieldDescription>
        </Field>
      ) : null}

      {value.role && value.scope_type === 'ORGANIZATION' ? (
        <OrgPicker label="Empresa do escopo" value={value.scope} onChange={(s) => onChange({ ...value, scope: s })} required />
      ) : null}
      {value.role && value.scope_type === 'ESTABLISHMENT' ? (
        <EstablishmentPicker label="Estabelecimento do escopo" value={value.scope} onChange={(s) => onChange({ ...value, scope: s })} required />
      ) : null}
      {value.role && value.scope_type === 'CITY' ? (
        <div className="grid grid-cols-1 gap-5 sm:grid-cols-3">
          <Field required>
            <FieldLabel>UF</FieldLabel>
            <Select value={value.uf} onValueChange={(uf) => onChange({ ...value, uf })}>
              <SelectTrigger>
                <SelectValue placeholder="UF" />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {UFS.map((uf) => (
                    <SelectItem key={uf} value={uf}>
                      {uf}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </Field>
          <Field required className="sm:col-span-2">
            <FieldLabel>Cidade</FieldLabel>
            <Input value={value.city} onChange={(e) => onChange({ ...value, city: e.target.value })} />
          </Field>
        </div>
      ) : null}
      {problem ? (
        <Field invalid>
          <FieldError>{problem}</FieldError>
        </Field>
      ) : null}
    </div>
  )
}
