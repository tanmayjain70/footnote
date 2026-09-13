/**
 * One fetch wrapper, one error shape.
 *
 * The API always answers failures as `{error: {code, message, detail}}`, so the
 * UI can branch on `code` and never has to match on prose. `ApiError` carries
 * the code through to whatever rendered the component.
 */

const BASE = import.meta.env.VITE_API_BASE ?? '/api/v1'

export class ApiError extends Error {
  code: string
  status: number
  detail: unknown

  constructor(status: number, code: string, message: string, detail?: unknown) {
    super(message)
    this.status = status
    this.code = code
    this.detail = detail
  }
}

let accessToken: string | null = null
let onUnauthorized: (() => void) | null = null

export function setAccessToken(token: string | null) {
  accessToken = token
}

export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler
}

function authHeaders(): Record<string, string> {
  return accessToken ? { Authorization: `Bearer ${accessToken}` } : {}
}

/** Turns a failed response into an ApiError, reading the body's error envelope when there is one. */
async function errorFrom(response: Response): Promise<ApiError> {
  let code = 'http_error'
  let message = `Request failed (${response.status})`
  let detail: unknown
  try {
    const body = await response.json()
    if (body?.error) {
      code = body.error.code ?? code
      message = body.error.message ?? message
      detail = body.error.detail
    }
  } catch {
    /* a non-JSON error body is still an error */
  }
  return new ApiError(response.status, code, message, detail)
}

function noteUnauthorized(response: Response) {
  if (response.status === 401 && accessToken) {
    // The token expired mid-session. Bounce to the login screen rather than
    // letting every panel render its own "unauthorized" message.
    onUnauthorized?.()
  }
}

function isAbort(err: unknown): boolean {
  return err instanceof DOMException && err.name === 'AbortError'
}

interface RequestOptions {
  method?: string
  body?: unknown
  formData?: FormData
  signal?: AbortSignal
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers = authHeaders()
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'

  const response = await fetch(`${BASE}${path}`, {
    method: options.method ?? (options.body || options.formData ? 'POST' : 'GET'),
    headers,
    body: options.formData ?? (options.body === undefined ? undefined : JSON.stringify(options.body)),
    signal: options.signal,
  })

  noteUnauthorized(response)
  if (!response.ok) throw await errorFrom(response)

  if (response.status === 204) return undefined as T
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) return (await response.json()) as T
  return (await response.text()) as unknown as T
}

export function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

/**
 * Downloads go through fetch rather than a bare link so the Authorization
 * header travels with them. A plain <a href> would arrive unauthenticated.
 */
export async function download(path: string, filename: string): Promise<void> {
  const response = await fetch(`${BASE}${path}`, { headers: authHeaders() })
  noteUnauthorized(response)
  if (!response.ok) throw await errorFrom(response)
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

export interface StreamHandlers {
  /** Called once per server-sent event with the event name and its parsed JSON data. */
  onEvent: (name: string, data: unknown) => void
  signal?: AbortSignal
}

/** Events after which the server sends nothing more, so the reader can stop. */
const TERMINAL_EVENTS = new Set(['done', 'error'])

interface ParsedEvent {
  name: string
  data: unknown
}

/**
 * One SSE block (the text between two blank lines) to an event. Comment lines
 * start with a colon; `id:` and `retry:` are legal but this API never sends
 * them. Multiple `data:` lines join with newlines, as the spec says.
 */
function parseEvent(raw: string): ParsedEvent | null {
  let name = 'message'
  const data: string[] = []
  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') name = value
    else if (field === 'data') data.push(value)
  }
  if (data.length === 0) return null
  const text = data.join('\n')
  try {
    return { name, data: JSON.parse(text) }
  } catch {
    // The API only ever sends JSON, but a caller can still see what arrived.
    return { name, data: text }
  }
}

/**
 * POST and read the response as a stream of server-sent events, handing each
 * one to `onEvent` as it arrives. Resolves when the server sends `done` or
 * `error`, when the body ends, or when `signal` aborts (quietly -- the caller
 * asked for that).
 *
 * A non-2xx response is thrown as an ApiError from the JSON body before any
 * event fires; that is how a 429 `budget_exhausted` arrives. Events split
 * across network chunks are reassembled, so `text` deltas are never torn.
 */
export async function streamPost<T = unknown>(
  path: string,
  body: T,
  handlers: StreamHandlers,
): Promise<void> {
  const headers = authHeaders()
  headers['Content-Type'] = 'application/json'
  headers.Accept = 'text/event-stream'

  let response: Response
  try {
    response = await fetch(`${BASE}${path}`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal: handlers.signal,
    })
  } catch (err) {
    if (isAbort(err)) return
    throw err
  }

  noteUnauthorized(response)
  if (!response.ok) throw await errorFrom(response)
  if (!response.body) {
    throw new ApiError(response.status, 'no_stream', 'The server did not stream a response.')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const deliver = (raw: string): boolean => {
    const event = parseEvent(raw)
    if (!event) return false
    handlers.onEvent(event.name, event.data)
    return TERMINAL_EVENTS.has(event.name)
  }

  try {
    for (;;) {
      const { value, done } = await reader.read()
      if (done) break
      // Normalise line endings on the whole buffer, not the chunk: a CR at the
      // end of one chunk and its LF at the start of the next must still pair.
      buffer = (buffer + decoder.decode(value, { stream: true })).replace(/\r\n/g, '\n')
      const blocks = buffer.split('\n\n')
      buffer = blocks.pop() ?? ''
      for (const block of blocks) {
        if (deliver(block)) {
          await reader.cancel().catch(() => undefined)
          return
        }
      }
    }
    // A final event the server did not terminate with a blank line.
    buffer += decoder.decode()
    if (buffer.trim()) deliver(buffer)
  } catch (err) {
    if (isAbort(err)) return
    throw err
  }
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => request<T>(path, { signal }),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  del: <T = void>(path: string) => request<T>(path, { method: 'DELETE' }),
  upload: <T>(path: string, formData: FormData) => request<T>(path, { formData }),
  download,
  streamPost,
}
