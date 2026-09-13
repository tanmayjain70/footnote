import type { ReactNode } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AuthProvider, useAuth } from './lib/auth'
import { Layout } from './components/Layout'
import { Spinner } from './components/ui'
import { AskPage } from './pages/AskPage'
import { DocumentPage } from './pages/DocumentPage'
import { DocumentsPage } from './pages/DocumentsPage'
import { EvalsPage } from './pages/EvalsPage'
import { Login } from './pages/Login'
import { RegisterPage } from './pages/RegisterPage'
import { UsagePage } from './pages/UsagePage'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Ingestion and extraction happen in a background worker, so a screen
      // left open for a minute is behind. Coming back to the tab refreshes it;
      // screens that watch a job set a shorter interval of their own.
      refetchOnWindowFocus: true,
      staleTime: 10_000,
      retry: 1,
    },
  },
})

/**
 * A route the current role may not use sends the person home rather than
 * showing a 403. The API refuses the calls regardless; this only keeps the
 * screen from being a page of error notes.
 */
function Guard({ allowed, children }: { allowed: boolean; children: ReactNode }) {
  return allowed ? <>{children}</> : <Navigate to="/" replace />
}

function Shell() {
  const { me, loading } = useAuth()

  if (loading) {
    return (
      <div className="grid min-h-screen place-items-center">
        <Spinner label="Signing you in" />
      </div>
    )
  }

  if (!me) return <Login />

  const { permissions } = me

  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<AskPage />} />
        <Route path="questions/:id" element={<AskPage />} />
        <Route path="documents" element={<DocumentsPage />} />
        <Route path="documents/:id" element={<DocumentPage />} />
        <Route path="register" element={<RegisterPage />} />
        <Route
          path="evals"
          element={
            <Guard allowed={permissions.can_run_evals}>
              <EvalsPage />
            </Guard>
          }
        />
        <Route
          path="usage"
          element={
            <Guard allowed={permissions.can_see_usage}>
              <UsagePage />
            </Guard>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AuthProvider>
          <Shell />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  )
}
