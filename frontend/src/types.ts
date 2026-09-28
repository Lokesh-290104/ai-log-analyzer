// Mirrors the backend's Pydantic response models (backend/app/main.py).

export interface Bucket {
  start: string
  count: number
  errors: number
}

export interface TopErrorRow {
  rank: number
  signature: string
  count: number
  share_pct: number
  example: string
  services: string[]
  first_ts: string
  last_ts: string
}

export interface Overview {
  total_entries: number
  first_ts: string | null
  last_ts: string | null
  error_entries: number
  error_share_pct: number
  by_level: Record<string, number>
  by_service: Record<string, number>
  service_count: number
  timeline: { bucket_minutes: number | null; buckets: Bucket[]; peak: Bucket | null }
  top_errors: { total_matching: number; rows: TopErrorRow[] }
}

export interface ParseStats {
  total_lines: number
  parsed_entries: number
  blank_lines: number
  continuation_lines: number
  unparsed_lines: number
  unparsed_samples: { line_no: number; text: string }[]
}

export interface UploadInfo {
  id: string
  name: string
  created_at: string
  size_bytes: number
  parse_stats: ParseStats
  overview: Overview
}

export interface ToolCallTrace {
  kind: 'tool_call'
  id: number
  tool: string
  args: Record<string, unknown>
  reason: string
  result: Record<string, unknown>
  ms: number
}

export interface RejectedTrace {
  kind: 'rejected'
  stage: 'schema' | 'tool' | 'judge'
  problem: string
  raw: string
  answer?: string
}

export type TraceItem = ToolCallTrace | RejectedTrace

export interface Verification {
  ok: boolean
  checked_numbers: string[]
  unsupported_numbers: string[]
  unknown_evidence: number[]
  evidence: number[]
}

export interface Analysis {
  id: string
  upload_id: string
  created_at: string
  question: string
  status: 'answered' | 'failed'
  answer: string | null
  error_code: string | null
  error: string | null
  trace: TraceItem[]
  verification: Verification | null
  model: string
  llm_calls: number
  duration_ms: number
}
