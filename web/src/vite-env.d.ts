/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Where the API lives, including the /api/v1 prefix. Left unset in
   * development so the client falls back to the relative '/api/v1', which
   * Vite's dev proxy forwards to the local API. In production this is the
   * deployed API's full URL, e.g. https://footnote-api.onrender.com/api/v1
   *
   * Vite inlines this at BUILD time, so changing it requires a rebuild.
   */
  readonly VITE_API_BASE?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
