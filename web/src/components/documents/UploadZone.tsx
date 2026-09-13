import { useEffect, useRef, useState } from 'react'
import type { DragEvent, FormEvent } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ApiError, api } from '../../lib/api'
import type { DocumentType, MePortfolio, UploadResult } from '../../lib/types'
import { Button, Card, ErrorNote, Field, Input, Notice, Select, cx } from '../ui'
import { DOC_TYPES, fileSize } from './values'

function isPdf(file: File): boolean {
  return file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf')
}

/**
 * The upload form for people who may upload. The API decides whether a file is
 * new: the same PDF sent to the same portfolio twice comes back as the existing
 * document with `created: false`, and that case is shown as its own message
 * rather than as a success, because "uploaded" would be a lie and an error
 * would send someone looking for a problem that is not there.
 */
export function UploadZone({
  portfolios,
  defaultPortfolio,
  onUploaded,
}: {
  portfolios: MePortfolio[]
  /** Follows the list's portfolio filter, so a filtered list uploads into what it shows. */
  defaultPortfolio: string
  onUploaded: (result: UploadResult) => void
}) {
  const input = useRef<HTMLInputElement>(null)
  const [file, setFile] = useState<File | null>(null)
  const [portfolioId, setPortfolioId] = useState(defaultPortfolio || (portfolios[0]?.id ?? ''))
  const [title, setTitle] = useState('')
  const [docType, setDocType] = useState<DocumentType>('lease')
  const [dragging, setDragging] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<UploadResult | null>(null)

  useEffect(() => {
    if (defaultPortfolio) setPortfolioId(defaultPortfolio)
  }, [defaultPortfolio])

  const upload = useMutation({
    mutationFn: (form: FormData) => api.upload<UploadResult>('/documents', form),
    onSuccess: (uploaded) => {
      setResult(uploaded)
      setError(null)
      setFile(null)
      setTitle('')
      if (input.current) input.current.value = ''
      onUploaded(uploaded)
    },
    onError: (err) => {
      setResult(null)
      setError(err instanceof ApiError ? err.message : 'The upload did not complete.')
    },
  })

  function choose(chosen: File | null | undefined) {
    if (!chosen) return
    if (!isPdf(chosen)) {
      setFile(null)
      setError('Only PDF files can be uploaded.')
      return
    }
    setError(null)
    setResult(null)
    setFile(chosen)
  }

  function onDrop(event: DragEvent<HTMLButtonElement>) {
    event.preventDefault()
    setDragging(false)
    choose(event.dataTransfer.files[0])
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!file || !portfolioId) return
    const form = new FormData()
    form.append('file', file, file.name)
    form.append('portfolio_id', portfolioId)
    form.append('doc_type', docType)
    if (title.trim()) form.append('title', title.trim())
    upload.mutate(form)
  }

  return (
    <Card
      title="Upload a document"
      subtitle="A PDF with a text layer. Scanned documents need OCR, which is not part of this milestone."
    >
      <form onSubmit={submit} className="grid gap-4 lg:grid-cols-[1fr_300px]">
        <button
          type="button"
          onClick={() => input.current?.click()}
          onDragOver={(event) => {
            event.preventDefault()
            setDragging(true)
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
          className={cx(
            'flex min-h-36 flex-col items-center justify-center gap-1 rounded-lg border-2 border-dashed px-6 py-8 text-center transition',
            dragging
              ? 'border-brand-500 bg-brand-50'
              : 'border-slate-300 bg-slate-50 hover:border-brand-500 hover:bg-brand-50/40',
          )}
        >
          {file ? (
            <>
              <span className="text-sm font-medium text-slate-900">{file.name}</span>
              <span className="text-xs text-slate-500">
                {fileSize(file.size)} · click to choose a different file
              </span>
            </>
          ) : (
            <>
              <span className="text-sm font-medium text-slate-700">
                Drop a PDF here, or click to choose one
              </span>
              <span className="text-xs text-slate-500">One file at a time</span>
            </>
          )}
          <input
            ref={input}
            type="file"
            accept="application/pdf,.pdf"
            className="hidden"
            onChange={(event) => choose(event.target.files?.[0])}
          />
        </button>

        <div className="space-y-3">
          <Field label="Portfolio">
            <Select
              className="w-full"
              value={portfolioId}
              onChange={(event) => setPortfolioId(event.target.value)}
              required
            >
              {portfolios.map((portfolio) => (
                <option key={portfolio.id} value={portfolio.id}>
                  {portfolio.name}
                  {portfolio.confidential ? ' (confidential)' : ''}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Title" hint="Optional. Defaults to the file name.">
            <Input
              value={title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Unit 4, Meridian House — lease"
              maxLength={255}
            />
          </Field>
          <Field label="Type">
            <Select
              className="w-full"
              value={docType}
              onChange={(event) => setDocType(event.target.value as DocumentType)}
            >
              {DOC_TYPES.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          </Field>
          <Button
            type="submit"
            variant="primary"
            className="w-full"
            disabled={!file || !portfolioId || upload.isPending}
          >
            {upload.isPending ? 'Uploading…' : 'Upload'}
          </Button>
        </div>
      </form>

      {error && (
        <div className="mt-4">
          <ErrorNote>{error}</ErrorNote>
        </div>
      )}
      {result && (
        <div className="mt-4">
          {result.created ? (
            <Notice tone="good" title="Uploaded">
              <Link
                to={`/documents/${result.document.id}`}
                className="font-medium text-good-800 underline"
              >
                {result.document.title}
              </Link>{' '}
              is queued for processing. The list updates as its pages are read and indexed.
            </Notice>
          ) : (
            <Notice tone="note" title="Already uploaded">
              This file was already uploaded as{' '}
              <Link
                to={`/documents/${result.document.id}`}
                className="font-medium text-note-800 underline"
              >
                {result.document.title}
              </Link>
              . Nothing was added.
            </Notice>
          )}
        </div>
      )}
    </Card>
  )
}
