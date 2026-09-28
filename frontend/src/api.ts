import type { Analysis, UploadInfo } from './types'

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// FastAPI errors are {"detail": "text"} or, for validation errors, {"detail": [{msg, loc}, ...]}.
export function errorMessage(status: number, body: unknown): string {
  const detail = (body as { detail?: unknown } | null)?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: string }
    return first.msg ? `Invalid request: ${first.msg}` : 'Invalid request.'
  }
  if (status === 429) return 'Too many requests. Please wait a moment.'
  if (status >= 500) return 'The server had a problem. Please try again.'
  return `Request failed (${status}).`
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(path, init)
  } catch {
    throw new ApiError(0, 'Could not reach the server. Check your connection and try again.')
  }
  const body = await response.json().catch(() => null)
  if (!response.ok) throw new ApiError(response.status, errorMessage(response.status, body))
  return body as T
}

export function uploadLogs(file: Blob, name: string): Promise<UploadInfo> {
  const form = new FormData()
  form.append('file', file, name)
  form.append('name', name)
  return request('/api/uploads', { method: 'POST', body: form })
}

export function uploadSample(): Promise<UploadInfo> {
  return request('/api/uploads/sample', { method: 'POST' })
}

export function getUpload(id: string): Promise<UploadInfo> {
  return request(`/api/uploads/${encodeURIComponent(id)}`)
}

export function ask(uploadId: string, question: string): Promise<Analysis> {
  return request(`/api/uploads/${encodeURIComponent(uploadId)}/ask`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  })
}

export function listAnalyses(uploadId: string): Promise<Analysis[]> {
  return request(`/api/uploads/${encodeURIComponent(uploadId)}/analyses`)
}
