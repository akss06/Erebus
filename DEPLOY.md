# Deployment

Two pieces: the **frontend** (Vite/React, static) on **Vercel**, and the **backend**
(FastAPI + rasterio/Earth Engine) on a Python/Docker host (**Render**, Railway or Fly).
The backend cannot run on Vercel — its native deps (GDAL via rasterio) and the
long-running live analysis exceed serverless limits.

The frontend reads the backend URL from `VITE_API_BASE` at build time. The backend
reads its allowed browser origin(s) from `ALLOWED_ORIGINS`. Deploy the backend first
so you have its URL for the frontend build.

## 1. Backend → Render (Docker)

1. In Render: **New → Blueprint**, point it at this repo. It picks up `render.yaml`
   (service `erebus-api`, built from `Dockerfile`). Or **New → Web Service → Docker**
   manually if you prefer.
2. Set env vars on the service:
   - `ALLOWED_ORIGINS` = your Vercel URL (set after step 2; can start with `*` to test,
     then lock down).
   - **Live analysis only** (the precomputed viewer works without these):
     - `GFW_API_TOKEN` = Global Fishing Watch API token.
     - `GEE_SERVICE_ACCOUNT` = Earth Engine service-account email.
     - `GEE_SA_KEY_JSON` = the service account's key file **contents** (full JSON).
       (Create via Google Cloud → the EE-registered project → a service account with
       Earth Engine access; register it at signup.earthengine.google.com.)
3. Deploy. Health check: `GET /api/health` → `{"status":"ok", ...}`.
   Note the service URL, e.g. `https://erebus-api.onrender.com`.

Railway / Fly.io: use the same `Dockerfile`; set the same env vars; they inject `$PORT`.

## 2. Frontend → Vercel

1. In Vercel: **Add New → Project**, import this repo. The repo-root `vercel.json`
   builds `frontend/` and serves `frontend/dist` (framework preset: Other / Vite).
2. Project **Environment Variables**:
   - `VITE_API_BASE` = the backend URL from step 1 (no trailing slash),
     e.g. `https://erebus-api.onrender.com`.
3. Deploy. Copy the resulting Vercel URL.

## 3. Wire them together

- Put the Vercel URL into the backend's `ALLOWED_ORIGINS` (comma-separated for several)
  and redeploy the backend.
- If you changed `VITE_API_BASE` after the first frontend deploy, redeploy the frontend
  (it is baked in at build time).

## Notes

- **Precomputed viewer works without any secrets** — Tuticorin / Gulf / recent areas,
  crops, and the AIS-off simulation are served from committed data. Only **Run Analysis**
  needs `GFW_API_TOKEN` + the `GEE_*` service account; without them it fails cleanly
  (reported as unavailable, never as an empty-sea result).
- Dev is unchanged: `VITE_API_BASE` unset → the frontend calls `/api`, proxied to
  `localhost:8000` by `vite.config.ts`.
- Never commit secrets. `.env` is gitignored; set tokens in the host's env UI.
