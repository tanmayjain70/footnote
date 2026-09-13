import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api, setAccessToken, setUnauthorizedHandler } from './api'
import type { Me, TokenPair } from './types'

const REFRESH_KEY = 'footnote.refresh'

interface AuthState {
  me: Me | null
  loading: boolean
  signIn: (email: string, password: string) => Promise<void>
  signOut: () => void
  /** Re-reads /auth/me, e.g. after an upload changes a portfolio's document count. */
  reload: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [me, setMe] = useState<Me | null>(null)
  const [loading, setLoading] = useState(true)
  const queryClient = useQueryClient()

  const signOut = useCallback(() => {
    localStorage.removeItem(REFRESH_KEY)
    setAccessToken(null)
    setMe(null)
    // Everything cached belongs to the person who just left: their documents,
    // their questions, the review count in the header. The next person to
    // sign in on this tab must not see any of it, even for a moment.
    queryClient.clear()
  }, [queryClient])

  const reload = useCallback(async () => {
    setMe(await api.get<Me>('/auth/me'))
  }, [])

  const adopt = useCallback(
    async (tokens: TokenPair) => {
      // The refresh token is the only thing persisted. The access token stays in
      // memory, so closing the tab drops it -- a small thing, but it means a
      // stolen localStorage dump is worth less.
      localStorage.setItem(REFRESH_KEY, tokens.refresh_token)
      setAccessToken(tokens.access_token)
      await reload()
    },
    [reload],
  )

  const signIn = useCallback(
    async (email: string, password: string) => {
      const tokens = await api.post<TokenPair>('/auth/login', { email, password })
      await adopt(tokens)
    },
    [adopt],
  )

  useEffect(() => {
    setUnauthorizedHandler(signOut)
  }, [signOut])

  useEffect(() => {
    const stored = localStorage.getItem(REFRESH_KEY)
    if (!stored) {
      setLoading(false)
      return
    }
    api
      .post<TokenPair>('/auth/refresh', { refresh_token: stored })
      .then(adopt)
      .catch(() => signOut())
      .finally(() => setLoading(false))
  }, [adopt, signOut])

  const value = useMemo(
    () => ({ me, loading, signIn, signOut, reload }),
    [me, loading, signIn, signOut, reload],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
