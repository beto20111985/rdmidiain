/** Tipos do control plane (W-BIZ `/control-plane`). */
export interface CpInstance {
  id: string
  slug: string
  name: string
  kind: string
  base_url: string | null
  group_id: string | null
  status: string
  current_version: string | null
  last_deployed_version: string | null
  health_status: string
  health: Record<string, unknown>
  last_heartbeat_at: string | null
  license: Record<string, unknown>
  notes: string | null
  created_at: string | null
  instance_token?: string
  events?: Array<{ type: string; data: unknown; created_at: string | null }>
}

export interface CpGroup {
  id: string
  name: string
  description: string | null
}

export interface CpRelease {
  id: string
  version: string
  status: string
  image_ref: string | null
  changelog: string | null
  validated_on_instance_id: string | null
  validated_at: string | null
  published_at: string | null
  created_at: string | null
}

export interface CpTarget {
  id: string
  instance_id: string
  position: number
  from_version: string | null
  to_version: string | null
  status: string
  attempt: number
  detail: string | null
  started_at: string | null
  finished_at: string | null
}

export interface CpRollout {
  id: string
  release_id: string
  target_kind: 'UMA' | 'GRUPO' | 'TODAS' | string
  target_ref: string | null
  max_concurrency: number
  failure_policy: 'PARAR' | 'CONTINUAR' | string
  executor: 'NULO' | 'SIMULACAO_LOCAL' | string
  simulated: boolean
  status: string
  is_rollback: boolean
  rollback_of_id: string | null
  reason: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string | null
  targets?: CpTarget[]
}

export interface CpOverview {
  instances: number
  by_version: Record<string, number>
  by_health: Record<string, number>
  active_rollouts: number
  latest_published_version: string | null
  outdated: number
  execution: { real_deploy: boolean; detail: string }
}

export interface CpEvent {
  id: string
  type: string
  instance_id: string | null
  rollout_id: string | null
  release_id: string | null
  data: unknown
  actor_id: string | null
  created_at: string | null
}
