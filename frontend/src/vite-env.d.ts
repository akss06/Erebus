/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Base URL of the backend API (e.g. https://erebus-api.onrender.com). Empty in
  // dev so requests go to /api and are proxied to localhost:8000 by vite.config.
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
