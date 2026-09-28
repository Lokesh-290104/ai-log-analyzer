import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'
import { errorMessage } from './api'
import type { Analysis, UploadInfo } from './types'

const upload: UploadInfo = {
  id: '0f2c3b1e-0000-4000-8000-000000000001',
  name: 'sample-shop-incident.log',
  created_at: '2026-09-28T12:00:00Z',
  size_bytes: 276_639,
  parse_stats: {
    total_lines: 2899,
    parsed_entries: 2894,
    blank_lines: 0,
    continuation_lines: 4,
    unparsed_lines: 1,
    unparsed_samples: [{ line_no: 2899, text: '--- log rotated ---' }],
  },
  overview: {
    total_entries: 2894,
    first_ts: '2026-09-28T09:30:03.419Z',
    last_ts: '2026-09-28T11:30:02.297Z',
    error_entries: 193,
    error_share_pct: 6.7,
    by_level: { INFO: 2502, WARN: 199, ERROR: 192, FATAL: 1 },
    by_service: { 'api-gateway': 1052, payments: 539 },
    service_count: 6,
    timeline: {
      bucket_minutes: 5,
      buckets: [
        { start: '2026-09-28T10:40:00Z', count: 100, errors: 20 },
        { start: '2026-09-28T10:45:00Z', count: 120, errors: 60 },
      ],
      peak: { start: '2026-09-28T10:45:00Z', count: 120, errors: 60 },
    },
    top_errors: {
      total_matching: 193,
      rows: [
        {
          rank: 1,
          signature: 'Database timeout after <n>ms',
          count: 111,
          share_pct: 57.5,
          example: 'Database timeout after 5000ms',
          services: ['payments'],
          first_ts: '2026-09-28T10:42:04.910Z',
          last_ts: '2026-09-28T10:53:58.000Z',
        },
      ],
    },
  },
}

const answered: Analysis = {
  id: 'a1',
  upload_id: upload.id,
  created_at: '2026-09-28T12:01:00Z',
  question: 'Which service has the most errors?',
  status: 'answered',
  answer: 'payments has the most errors: 144 (74.6%).',
  error_code: null,
  error: null,
  trace: [
    {
      kind: 'tool_call',
      id: 1,
      tool: 'count_by',
      args: { field: 'service', level: ['ERROR', 'FATAL'] },
      reason: 'errors per service',
      result: { total_matching: 193, rows: [{ value: 'payments', count: 144, share_pct: 74.6 }] },
      ms: 3,
    },
  ],
  verification: { ok: true, checked_numbers: ['144', '74.6'], unsupported_numbers: [], unknown_evidence: [], evidence: [1] },
  model: 'gemma-4-26b-a4b-it',
  llm_calls: 2,
  duration_ms: 6400,
}

const failed: Analysis = {
  ...answered,
  id: 'a2',
  status: 'failed',
  answer: null,
  error_code: 'unverified_answer',
  error: "The model's answer contained numbers the tools did not produce, so it was rejected.",
  verification: null,
  trace: [
    answered.trace[0],
    { kind: 'rejected', stage: 'judge', problem: 'these numbers/times are not in the tool results you cited: 150', raw: '{}', answer: 'payments had 150 errors' },
  ],
}

type Route = { status?: number; body: unknown }
let routes: Record<string, Route | (() => Route)>
const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
  const key = `${init?.method ?? 'GET'} ${String(input)}`
  const entry = routes[key]
  if (!entry) throw new Error(`unexpected fetch ${key}`)
  const { status = 200, body } = typeof entry === 'function' ? entry() : entry
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
})

beforeEach(() => {
  window.history.replaceState(null, '', '/')
  routes = {
    'POST /api/uploads/sample': { status: 201, body: upload },
    [`GET /api/uploads/${upload.id}/analyses`]: { body: [] },
  }
  vi.stubGlobal('fetch', fetchMock)
  // Recharts measures its container; jsdom has no layout.
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  )
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
  fetchMock.mockClear()
})

async function openSample() {
  const user = userEvent.setup()
  render(<App />)
  await user.click(screen.getByRole('button', { name: /sample incident log/i }))
  await screen.findByRole('heading', { name: /what's in sample-shop-incident\.log/i })
  return user
}

describe('App', () => {
  it('shows the overview computed by code after uploading the sample', async () => {
    await openSample()
    expect(screen.getByText('2,894')).toBeInTheDocument()
    expect(screen.getByText('193')).toBeInTheDocument()
    expect(screen.getByText('6.7% of entries')).toBeInTheDocument()
    expect(screen.getByText('Database timeout after <n>ms')).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('1 line was not recognized')
    expect(window.location.hash).toBe(`#/u/${upload.id}`)
  })

  it('asks a question and shows the verified answer with its evidence', async () => {
    let resolve!: (r: Route) => void
    routes[`POST /api/uploads/${upload.id}/ask`] = () => ({ body: answered })
    const user = await openSample()
    const pending = new Promise<Route>((r) => (resolve = r))
    fetchMock.mockImplementationOnce(async () => {
      const { body } = await pending
      return new Response(JSON.stringify(body), { status: 200 })
    })

    await user.type(screen.getByLabelText(/question about the log/i), 'Which service has the most errors?')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    expect(screen.getByRole('button', { name: /analyzing/i })).toBeDisabled()
    expect(screen.getByText(/the agent is choosing tools/i)).toBeInTheDocument()

    resolve({ body: answered })
    const card = await screen.findByRole('article', { name: /which service has the most errors/i })
    expect(within(card).getByText(/payments has the most errors: 144/)).toBeInTheDocument()
    expect(within(card).getByText(/verified/i)).toHaveTextContent('(144, 74.6) found in tool result #1')
    expect(within(card).getByText('count_by')).toBeInTheDocument()
    expect(within(card).getByText('cited')).toBeInTheDocument()
    const request = JSON.parse(String(fetchMock.mock.calls.at(-1)?.[1]?.body))
    expect(request).toEqual({ question: 'Which service has the most errors?' })
  })

  it('shows a fail-closed result without any answer text', async () => {
    routes[`POST /api/uploads/${upload.id}/ask`] = { body: failed }
    const user = await openSample()
    await user.click(screen.getByRole('button', { name: 'When was the error spike at its peak?' }))
    const card = await screen.findByRole('article')
    expect(within(card).getByRole('alert')).toHaveTextContent('No answer shown (failed closed)')
    expect(within(card).getByText(/Answer rejected by the judge/)).toBeInTheDocument()
    expect(within(card).getByText(/Rejected answer: “payments had 150 errors”/)).toBeInTheDocument()
  })

  it('shows API errors such as rate limiting', async () => {
    routes[`POST /api/uploads/${upload.id}/ask`] = {
      status: 429,
      body: { detail: 'Too many questions: at most 6 per minute. Please wait a moment.' },
    }
    const user = await openSample()
    await user.type(screen.getByLabelText(/question about the log/i), 'errors?')
    await user.click(screen.getByRole('button', { name: 'Ask' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('at most 6 per minute')
    expect(screen.getByRole('button', { name: 'Ask' })).toBeEnabled()
  })

  it('shows upload errors from the server', async () => {
    routes['POST /api/uploads'] = { status: 400, body: { detail: 'No log lines were recognized.' } }
    const user = userEvent.setup()
    render(<App />)
    await user.type(screen.getByLabelText(/paste log lines/i), 'hello')
    await user.click(screen.getByRole('button', { name: /analyze pasted text/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('No log lines were recognized.')
  })

  it('rejects files over 5 MB before uploading', async () => {
    const user = userEvent.setup()
    const { container } = render(<App />)
    const big = new File(['x'], 'big.log')
    Object.defineProperty(big, 'size', { value: 6_000_000 })
    await user.upload(container.querySelector('input[type=file]') as HTMLInputElement, big)
    expect(screen.getByRole('alert')).toHaveTextContent('larger than 5 MB')
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('reopens the upload from the URL hash', async () => {
    window.history.replaceState(null, '', `/#/u/${upload.id}`)
    routes[`GET /api/uploads/${upload.id}`] = { body: upload }
    render(<App />)
    expect(await screen.findByRole('heading', { name: /what's in/i })).toBeInTheDocument()
  })

  it('reports a network failure clearly', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const user = userEvent.setup()
    render(<App />)
    await user.click(screen.getByRole('button', { name: /sample incident log/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not reach the server')
  })
})

describe('errorMessage', () => {
  it('reads FastAPI error shapes', () => {
    expect(errorMessage(400, { detail: 'bad' })).toBe('bad')
    expect(errorMessage(422, { detail: [{ msg: 'too short' }] })).toBe('Invalid request: too short')
    expect(errorMessage(502, null)).toMatch(/server had a problem/)
    expect(errorMessage(429, {})).toMatch(/Too many requests/)
  })
})
