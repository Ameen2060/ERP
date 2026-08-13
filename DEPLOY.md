# Deploying the Accounting / ERP app

## Deployment status (last updated by automated prep)

| Item | Status |
|------|--------|
| Repo | Local git initialized, branch **`main`**, first commit done. **Not yet pushed** — needs your GitHub account. |
| Target repo name | `erp-ameen` |
| Deploy files | ✅ `Dockerfile`, `render.yaml`, `Procfile`, `vercel.json`, `.gitignore`, `.dockerignore`, `DEPLOY.md`, `api/index.py` — all present & validated |
| Secrets in repo | ✅ None. `data/`, `*.sqlite3`, backups, `.env`, `.venv` are git-ignored and unstaged |
| Production-config E2E | ✅ Passed locally with `AUTH_ENABLED=true`: login, auth gating, dashboard, seeded CoA (41), create invoice+payment, vendor bill, document upload, AR-aging / income-statement / VAT-return reports, invoice PDF, xlsx export, e-invoice generate→submit (sample sandbox), SPA/static, 404/401 handling |
| Persistence | ✅ Configured — SQLite + uploads on a mounted disk at `/data` (survives restarts & redeploys) |
| Hosting URL | ⏳ Created after you deploy — will be `https://erp-ameen.onrender.com` |
| **Blocked (needs your login)** | Creating/pushing the GitHub repo, and clicking Deploy in Render. No CLI/token for either is available on the build machine. |

### Environment variables to set in Render
`SECRET_KEY` auto-generates via `render.yaml`. You **must** set **`ADMIN_PASSWORD`** (marked
`sync:false`). All others are pre-set in `render.yaml`/`Dockerfile`. First login is
`admin` / your `ADMIN_PASSWORD`; change it in-app immediately.

> On a fresh production database the e-invoicing provider defaults to **`manual`**. Switch it to
> **`sample`** in *E-Invoice Settings* if you want the sandbox submit→accepted flow there.

---

This app is a **single stateful FastAPI process** (API + web UI on one port) backed by a
**SQLite** file plus uploaded files (attachments, org logo). The cleanest way to run it live and
keep data is a host that runs a persistent container with a **mounted disk** — Render, Railway,
or Fly.io. Vercel is **not** a real fit (see the caveat at the bottom).

The repo is already deploy-ready:

| File | Purpose |
|------|---------|
| `Dockerfile` | Builds the app; runs `uvicorn` on the platform `$PORT`; data lives at `/data`. |
| `render.yaml` | One-click Render blueprint: web service + 1 GB persistent disk + env vars. |
| `Procfile` | Start command for Railway / Heroku-style hosts. |
| `vercel.json` + `api/index.py` | Vercel demo path (non-persistent — read the caveat). |

## Before you deploy — production settings (important)

The app ships with dev defaults. The Docker image already forces the safe ones
(`AUTH_ENABLED=true`, `RESET_EXPOSE_TOKEN=false`, data under `/data`). You must still set:

- **`SECRET_KEY`** — a long random string (Render's blueprint auto-generates it).
- **`ADMIN_PASSWORD`** — the first admin password. First login is `admin` / this value; change it
  in-app afterwards. Never commit it.

Everything else (VAT treatments, e-invoicing config on the **sample** sandbox provider, etc.)
is seeded automatically on first boot.

---

## Option A — Render (recommended, ~5 min)

1. Push this `Accounting` folder to a GitHub repo (see **Push to GitHub** below).
2. Go to <https://dashboard.render.com/blueprints> → **New Blueprint Instance** → pick the repo.
   Render reads `render.yaml` and provisions the web service + disk.
3. When prompted, set **`ADMIN_PASSWORD`** (the one env var marked `sync: false`).
4. Deploy. You get a live URL like **`https://erp-ameen.onrender.com`**.
5. **Custom domain / your requested address:** Render gives `*.onrender.com`. To use
   `erp.ameen.<yourdomain>` you must own a domain — add it under **Settings → Custom Domains**
   and create the DNS record Render shows. (A bare `erp.ameen.vercel.com` is not obtainable —
   `*.vercel.com` is Vercel's own domain and is never issued to users.)

## Option B — Railway

1. Push to GitHub (below).
2. <https://railway.app> → **New Project → Deploy from GitHub repo**. Railway detects the
   `Dockerfile`.
3. Add a **Volume** mounted at `/data` (Railway → service → Variables/Volumes).
4. Set variables: `SECRET_KEY`, `ADMIN_PASSWORD`, `AUTH_ENABLED=true`,
   `RESET_EXPOSE_TOKEN=false`, `DATABASE_URL=sqlite:////data/accounting.sqlite3`,
   `ATTACHMENTS_DIR=/data/attachments`, `ORG_DIR=/data/org`.
5. Deploy → live at `https://<name>.up.railway.app`. Add a custom domain in the service settings.

## Option C — Fly.io

```bash
fly launch --no-deploy          # detects the Dockerfile; keep the app name e.g. erp-ameen
fly volumes create data --size 1 --region dxb   # dxb = Dubai
# In fly.toml add:  [mounts]  source="data"  destination="/data"
fly secrets set SECRET_KEY=$(openssl rand -hex 32) ADMIN_PASSWORD=YourStrongPass
fly deploy
```
Live at `https://erp-ameen.fly.dev`; `fly certs add erp.yourdomain.com` for a custom domain.

---

## Push to GitHub (needed for A and B)

```bash
cd "C:/Users/AmeenAboSallow/Downloads/Accounting"
git init && git add -A && git commit -m "Accounting/ERP app — deployable"
git branch -M main
git remote add origin https://github.com/<you>/erp-ameen.git
git push -u origin main
```
> Make sure `data/` is git-ignored so your local database/backups aren't published. Add a
> `.gitignore` with `data/` and `.venv/` if you don't have one.

## Verify after deploy

- `https://<your-url>/health` → `{"status":"ok"}`
- Open the site, log in as `admin` / your `ADMIN_PASSWORD`, change the password.
- The app boots with a seeded Chart of Accounts and the e-invoicing module on the **sample**
  sandbox provider (still marked *Provisional — requires UAE SME validation*).

---

## Why not Vercel (the honest caveat)

`vercel.json` + `api/index.py` will run the app on Vercel, **but Vercel is serverless with a
read-only filesystem** — the SQLite database can't persist, so data resets on every cold start.
Use it only for a throwaway demo. For real use on Vercel you'd have to move the database to an
external managed Postgres (e.g. Neon / Vercel Postgres) and adapt the SQLite-specific startup
migrations — that's a separate change, not covered here. The Docker + persistent-disk options
above give a genuinely working, data-persistent deployment with far less effort.

To deploy the demo anyway: `vercel` (from this folder, with the Vercel CLI + your account) →
`https://erp-ameen.vercel.app`.
