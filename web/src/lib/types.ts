/*
 * The API's shapes, one for one. Field names are the API's field names, snake
 * case included, so a response can be rendered without a mapping layer and a
 * change on one side shows up as a type error on the other.
 *
 * Money from the model provider (`cost_usd`) arrives as a decimal string, not
 * a number: the API stores six decimal places and a JS number would round
 * them. Dates are ISO strings; date-only values (`term_end`) have no time.
 */

export type Role = 'director' | 'admin' | 'manager' | 'viewer'

export type DocumentStatus = 'queued' | 'processing' | 'ready' | 'failed'
export type DocumentType = 'lease' | 'deed_of_variation' | 'side_letter' | 'notice' | 'other'
export type JobKind = 'ingest' | 'extract' | 'eval_run'
export type JobStatus = 'queued' | 'running' | 'done' | 'failed'
/** `none` when the document has never been extracted. */
export type ExtractionStatus = 'none' | 'queued' | 'running' | 'done' | 'failed'
export type QuestionStatus = 'answered' | 'unanswered' | 'failed' | 'budget_exhausted'
export type ReviewStatus = 'pending' | 'confirmed' | 'corrected' | 'rejected'
export type ReviewAction = 'confirm' | 'correct' | 'reject'
export type Confidence = 'high' | 'medium' | 'low'
export type ValueIssue = 'no_evidence' | 'invalid_chunk' | 'quote_not_found' | 'unparseable'
export type FieldType = 'text' | 'date' | 'money' | 'integer' | 'bool' | 'enum'
export type EvalMode = 'retrieval' | 'end_to_end'
export type EvalRunStatus = 'running' | 'done' | 'failed'
export type EvalSource = 'generated' | 'feedback' | 'manual'
export type UsageKind = 'answer' | 'extraction' | 'eval' | 'embedding'
export type FeedbackVerdict = 'up' | 'down'
export type FeedbackReason =
  | 'wrong'
  | 'missing_citation'
  | 'incomplete'
  | 'should_have_refused'
  | 'other'

// ---------------------------------------------------------------------------
// Auth

export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export interface User {
  id: string
  email: string
  full_name: string
  role: Role
}

/**
 * What this person may do, resolved server-side. The UI uses it to decide
 * what to render; the API enforces it again regardless.
 */
export interface Permissions {
  can_upload: boolean
  can_ask: boolean
  can_review: boolean
  can_run_evals: boolean
  can_see_usage: boolean
}

export interface MePortfolio {
  id: string
  name: string
  slug: string
  confidential: boolean
  document_count: number
}

export interface Me {
  user: User
  permissions: Permissions
  portfolios: MePortfolio[]
}

// ---------------------------------------------------------------------------
// Shared

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

/** The one error shape every failure comes back in. */
export interface ApiErrorBody {
  error: { code: string; message: string; detail?: unknown }
}

export interface Portfolio {
  id: string
  name: string
  slug: string
  confidential: boolean
  document_count: number
  ready_count: number
}

export interface Health {
  worker_alive: boolean
  jobs: { queued: number; running: number; failed: number }
  documents: { ready: number; processing: number; failed: number }
  llm: { provider: string; model: string }
  embeddings: { provider: string; model: string; dimensions: number }
  /** Null for every role but the director: spend is theirs to see. */
  budget: { daily_budget_usd: string; spent_today_usd: string } | null
}

// ---------------------------------------------------------------------------
// Documents

export interface DocumentOut {
  id: string
  portfolio_id: string
  portfolio_name: string
  title: string
  filename: string
  doc_type: DocumentType
  status: DocumentStatus
  error: string | null
  page_count: number
  chunk_count: number
  byte_size: number
  uploaded_by_name: string | null
  metadata: Record<string, unknown>
  created_at: string
  updated_at: string
  extraction_status: ExtractionStatus
  /** Extracted values still waiting for a person to confirm them. */
  review_pending: number
}

/** POST /documents. `created` is false when the same PDF was already in the portfolio. */
export interface UploadResult {
  document: DocumentOut
  created: boolean
}

export interface DocumentPageOut {
  page_number: number
  text: string
}

export interface ChunkOut {
  id: string
  page_number: number
  ordinal: number
  text: string
}

/** GET /chunks/{id}: a chunk plus the sentence blocks a citation's range indexes into. */
export interface ChunkDetail extends ChunkOut {
  document_id: string
  document_title: string
  blocks: string[]
}

// ---------------------------------------------------------------------------
// Ask

export interface AskRequest {
  question: string
  portfolio_id?: string | null
}

/** One passage the model was shown, in the order it was shown. `index` is what the model cites by. */
export interface SourceOut {
  index: number
  chunk_id: string
  document_id: string
  document_title: string
  page_number: number
  snippet: string
  fused_score: number
  vector_rank: number | null
  lexical_rank: number | null
  cited: boolean
}

/** A verified citation. `ordinal` is the [n] label; the block range is within the chunk's sentence blocks, end exclusive. */
export interface Citation {
  ordinal: number
  chunk_id: string
  document_id: string
  document_title: string
  page_number: number
  cited_text: string
  source_index: number
  block_start: number
  block_end: number
}

export interface AnswerBlock {
  block: number
  text: string
  /** Citation ordinals attached to this block, in the order they were made. */
  citations: number[]
}

export interface Feedback {
  verdict: FeedbackVerdict
  reason: FeedbackReason | null
  note: string | null
}

export interface FeedbackRequest {
  verdict: FeedbackVerdict
  reason?: FeedbackReason | null
  note?: string | null
}

export interface AnswerOut {
  id: string
  user_id: string | null
  user_name: string | null
  portfolio_id: string | null
  text: string
  status: QuestionStatus
  answer_text: string | null
  answer_blocks: AnswerBlock[] | null
  citations: Citation[]
  sources: SourceOut[]
  provider: string
  model: string
  latency_ms: number | null
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cost_usd: string
  /** Citations the model made that did not match a passage it was shown. Counted, never rendered. */
  dropped_citations: number
  error: string | null
  /** The caller's own feedback, if any. */
  feedback: Feedback | null
  created_at: string
  finished_at: string | null
}

/** A stored question is the same shape as a finished answer. */
export type QuestionOut = AnswerOut

// ---------------------------------------------------------------------------
// SSE events from POST /ask. `streamPost` hands these over by name.

export interface RetrievalSource {
  index: number
  chunk_id: string
  document_id: string
  document_title: string
  page_number: number
  snippet: string
  vector_rank: number | null
  lexical_rank: number | null
  fused_score: number
}

export interface RetrievalEvent {
  question_id: string
  sources: RetrievalSource[]
}

export interface TextEvent {
  block: number
  text: string
}

export interface CitationEvent {
  block: number
  ordinal: number
  source_index: number
  chunk_id: string
  document_id: string
  document_title: string
  page_number: number
  cited_text: string
}

export type DoneEvent = AnswerOut

export interface ErrorEvent {
  code: string
  message: string
}

export type AskEventName = 'retrieval' | 'text' | 'citation' | 'done' | 'error'

export type AskEvent =
  | { event: 'retrieval'; data: RetrievalEvent }
  | { event: 'text'; data: TextEvent }
  | { event: 'citation'; data: CitationEvent }
  | { event: 'done'; data: DoneEvent }
  | { event: 'error'; data: ErrorEvent }

// ---------------------------------------------------------------------------
// Extraction and review

/** One entry of the field registry (GET /fields). */
export interface FieldSpec {
  key: string
  label: string
  type: FieldType
  description: string
  enum_values: string[]
}

/** The typed form of a value: ISO date string, number, boolean or text. */
export type ExtractedJson = string | number | boolean | null

export interface ExtractedValueOut {
  id: string
  field_key: string
  label: string
  type: FieldType
  value_json: ExtractedJson
  /** As the model wrote it, before parsing. */
  value_text: string | null
  /** The corrected value when there is one, else the extracted value. */
  effective_value: ExtractedJson
  quote: string | null
  chunk_id: string | null
  page_number: number | null
  confidence: Confidence
  issue: ValueIssue | null
  review_status: ReviewStatus
  corrected_json: ExtractedJson
  reviewed_by_name: string | null
  reviewed_at: string | null
  review_note: string | null
}

export interface ExtractionOut {
  id: string
  document_id: string
  status: JobStatus
  provider: string
  model: string
  schema_version: number
  started_at: string | null
  finished_at: string | null
  error: string | null
  cost_usd: string
  values: ExtractedValueOut[]
}

export interface ReviewRequest {
  action: ReviewAction
  corrected_value?: string | null
  note?: string | null
}

/** GET /review-queue: documents with values still waiting on a person. */
export interface ReviewQueueItem {
  document_id: string
  title: string
  portfolio_name: string
  pending: number
  /** Pending values that also carry an `issue`. */
  issues: number
}

// ---------------------------------------------------------------------------
// Register

export interface RegisterReview {
  confirmed: number
  pending: number
  corrected: number
  rejected: number
}

/**
 * One document's key terms. Unreviewed values are null unless the request
 * asked for `include_unreviewed`, because a value nobody has confirmed does
 * not count.
 */
export interface RegisterRow {
  document_id: string
  title: string
  portfolio: string
  tenant_name: string | null
  unit: string | null
  property_address: string | null
  term_start: string | null
  term_end: string | null
  annual_rent_gbp: number | null
  rent_review_basis: string | null
  rent_review_date: string | null
  break_date: string | null
  break_notice_months: number | null
  repairing_obligation: string | null
  review: RegisterReview
  /** Every non-null value has been reviewed. */
  complete: boolean
}

export interface RegisterCounts {
  documents: number
  complete: number
  pending_values: number
}

export interface RegisterResponse {
  rows: RegisterRow[]
  counts: RegisterCounts
}

// ---------------------------------------------------------------------------
// Evals

export interface EvalQuestion {
  id: string
  question: string
  answerable: boolean
  expected_document_id: string | null
  expected_document_title: string | null
  expected_pages: number[]
  portfolio_id: string | null
  source: EvalSource
  active: boolean
  notes: string | null
  created_at: string
  updated_at: string
}

export interface EvalQuestionCreate {
  question: string
  answerable: boolean
  expected_document_id?: string | null
  expected_pages?: number[]
  portfolio_id?: string | null
  notes?: string | null
}

export interface EvalQuestionFromQuestion {
  question_id: string
  answerable: boolean
  expected_document_id?: string | null
  expected_pages?: number[]
}

export interface EvalRunRequest {
  mode: EvalMode
  limit?: number | null
}

/** Rates are fractions in 0..1; `mrr` is mean reciprocal rank. End-to-end runs add the answer fields. */
export interface EvalTotals {
  questions?: number
  answerable?: number
  unanswerable?: number
  doc_hit_rate?: number | null
  page_hit_rate?: number | null
  mrr?: number | null
  answer_rate?: number | null
  citation_doc_hit_rate?: number | null
  citation_page_hit_rate?: number | null
  correct_refusal_rate?: number | null
  false_answer_rate?: number | null
  cost_usd?: string | number | null
  latency_p50_ms?: number | null
}

export interface EvalRun {
  id: string
  mode: EvalMode
  status: EvalRunStatus
  started_by_name: string | null
  config: Record<string, unknown>
  totals: EvalTotals
  started_at: string
  finished_at: string | null
  error: string | null
}

export interface EvalResult {
  eval_question_id: string
  question: string
  answerable: boolean
  expected_document_title: string | null
  expected_pages: number[]
  retrieved_doc_hit: boolean | null
  retrieved_page_hit: boolean | null
  hit_rank: number | null
  answered: boolean | null
  cited_doc_hit: boolean | null
  cited_page_hit: boolean | null
  refused_correctly: boolean | null
  question_id: string | null
  latency_ms: number | null
  cost_usd: string
}

export interface EvalRunDetail extends EvalRun {
  results: EvalResult[]
}

// ---------------------------------------------------------------------------
// Usage

export interface UsageDay {
  day: string
  cost_usd: string
  calls: number
}

export interface UsageByKind {
  kind: UsageKind
  cost_usd: string
  calls: number
}

export interface UsageByUser {
  user_id: string | null
  full_name: string | null
  cost_usd: string
  calls: number
}

export interface UsageSummary {
  spent_today_usd: string
  daily_budget_usd: string
  remaining_usd: string
  days: number
  by_day: UsageDay[]
  by_kind: UsageByKind[]
  by_user: UsageByUser[]
}

export interface BudgetRequest {
  daily_budget_usd: number | string
}
