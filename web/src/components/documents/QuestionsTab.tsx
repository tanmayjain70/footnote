import { Link } from 'react-router-dom'
import { cost, count, dateTime } from '../../lib/format'
import type { QuestionOut } from '../../lib/types'
import { Card, Empty, Spinner, StatusPill, Table, Td, Th } from '../ui'

/** The pages of this document an answer cited, in order, once each. */
function citedPages(question: QuestionOut, documentId: string): number[] {
  const pages = (question.citations ?? [])
    .filter((citation) => citation.document_id === documentId)
    .map((citation) => citation.page_number)
  return [...new Set(pages)].sort((a, b) => a - b)
}

/**
 * Questions whose answer cited this document. The API applies the same rule as
 * everywhere else -- own questions unless you are the director -- so the list
 * is not the document's full history, and the subtitle says so.
 */
export function QuestionsTab({
  documentId,
  questions,
  loading,
}: {
  documentId: string
  questions: QuestionOut[] | undefined
  loading: boolean
}) {
  if (loading) return <Spinner label="Loading questions" />
  if (!questions?.length) {
    return (
      <Empty title="No questions have cited this document">
        When an answer cites a passage from this document, the question is listed here.
      </Empty>
    )
  }

  return (
    <Card
      title={`${count(questions.length)} question${questions.length === 1 ? '' : 's'}`}
      subtitle="Answers that cited a passage from this document. Only questions you may see are listed."
    >
      <Table>
        <thead>
          <tr>
            <Th>Asked</Th>
            <Th>Question</Th>
            <Th>Status</Th>
            <Th>Asked by</Th>
            <Th>Cited here</Th>
            <Th right>Cost</Th>
          </tr>
        </thead>
        <tbody>
          {questions.map((question) => {
            const pages = citedPages(question, documentId)
            return (
              <tr key={question.id} className="hover:bg-slate-50">
                <Td className="whitespace-nowrap text-slate-500">{dateTime(question.created_at)}</Td>
                <Td className="max-w-lg">
                  <Link
                    to={`/questions/${question.id}`}
                    className="line-clamp-2 font-medium text-slate-900 hover:text-brand-700"
                  >
                    {question.text}
                  </Link>
                </Td>
                <Td>
                  <StatusPill status={question.status} title={question.error ?? undefined} />
                </Td>
                <Td className="whitespace-nowrap">{question.user_name ?? '—'}</Td>
                <Td className="tabular whitespace-nowrap text-slate-600">
                  {pages.length
                    ? `p. ${pages.join(', ')}`
                    : question.citations?.length
                      ? 'other documents'
                      : '—'}
                </Td>
                <Td right className="text-xs text-slate-500">
                  {cost(question.cost_usd)}
                </Td>
              </tr>
            )
          })}
        </tbody>
      </Table>
    </Card>
  )
}
