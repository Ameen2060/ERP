# Deploying the Accounting / ERP app

> **Chosen target: Vercel.** The app has been adapted to run on Vercel's Python runtime with a
> hosted **Postgres** database (Vercel can't keep a SQLite file). See **Deploy on Vercel** below.
> The Docker / persistent-disk options (Render/Railway/Fly) remain available and are simpler if
> you ever want an all-in-one host — they're kept further down.

## Deployment status (last updated by automated prep)

| Item | Status |
|------|--------|
| Repo | Local git initialized, branch **`main`**, committed. **Not yet pushed** — needs your GitHub account. |
| Target repo name | `erp-ameen` |
| Deploy files | ✅ `vercel.json` + `api/index.py` (Vercel), `Dockerfile`/`render.yaml`/`Procfile` (persistent-host), `.gitignore`, `DEPLOY.md` — all present & validated |
| Code readiness | ✅ Postgres auto-detected from `POSTGRES_URL`; DB init runs on serverless cold start; `postgres://` URLs normalized to `postgresql+psycopg`; `psycopg[binary]` added to requirements |
| Secrets in repo | ✅ None. `data/`, `*.sqlite3`, backups, `.env`, `.venv` are git-ignored and unstaged |
| Test suite | ✅ 221 passed after the Vercel/Postgres refactor (SQLite path unchanged) |
| Production-config E2E | ✅ Passed with `AUTH_ENABLED=true`: login, gating, dashboard, seeded CoA (41), invoice+payment, vendor bill, document upload, AR-aging/income-statement/VAT-return, invoice PDF, xlsx export, e-invoice generate→submit (sample), SPA/static, 404/401 |
| Hosting URL | ⏳ Created after you deploy — `https://erp-ameen.vercel.app` |
| **Blocked (needs your login)** | Push to GitHub, add Vercel Postgres, and click Deploy in Vercel. No CLI/token for GitHub or Vercel is available on the build machine. |

### Vercel constraints (be aware)
- **Database = Postgres, not SQLite.** Add **Vercel Postgres** (Storage tab) — it sets
  `POSTGRES_URL`, which the app auto-detects. Without it, data will not persist.
- **Uploaded files are ephemeral on Vercel.** Attachments/logo write under `/tmp` and don't
  survive between invocations. Accounting data (Postgres) persists. Durable file storage would
  need Vercel Blob (a later change).
- **Function size.** The PDF/Excel/parse dependencies are sizeable; if a Vercel build hits the
  serverless size limit, drop `pdfplumber` and `xlrd` from `requirements.txt` (only the
  TB/GL-import PDF/XLS parsing feature needs them).

---

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

## Deploy on Vercel (chosen path)

The app is already adapted for Vercel: `vercel.json` serves `api/index.py` (the ASGI app),
which initializes the database on cold start; `POSTGRES_URL` is auto-detected; `psycopg` is in
`requirements.txt`.

1. **Push to GitHub** (see *Push to GitHub* above) — repo `erp-ameen`.
2. **Import to Vercel:** <https://vercel.com/new> → **Import Git Repository** → pick `erp-ameen`.
   Framework preset: **Other**. Deploy once (it will run, but has no database yet).
3. **Add a database:** project → **Storage** → **Create Database** → **Postgres** (or connect
   **Neon**). Link it to the project. Vercel injects `POSTGRES_URL` automatically — the app
   picks it up with no code change.
4. **Set Environment Variables** (project → Settings → Environment Variables), then redeploy:

   | Variable | Value |
   |----------|-------|
   | `SECRET_KEY` | a long random string (e.g. `openssl rand -hex 32`) |
   | `ADMIN_PASSWORD` | your chosen admin password (do NOT commit it) |
   | `AUTH_ENABLED` | `true` |
   | `RESET_EXPOSE_TOKEN` | `false` |
   | `ATTACHMENTS_DIR` | `/tmp/attachments` |
   | `ORG_DIR` | `/tmp/org` |
   | `SEED_ON_STARTUP` | `true` |

   (`POSTGRES_URL` is provided by the linked database — you don't set it by hand.)
5. **Redeploy** (Deployments → ⋯ → Redeploy) so the env vars + database take effect.
6. Live at **`https://erp-ameen.vercel.app`**. First login: `admin` / your `ADMIN_PASSWORD` —
   change it immediately. Add a custom domain under **Settings → Domains** if you own one.

### Verify after deploy
- `https://erp-ameen.vercel.app/health` → `{"status":"ok"}`
- Log in; the Chart of Accounts is seeded; e-invoicing is on the **sample** sandbox provider
  (switch it in *E-Invoice Settings* if needed) — still marked *Provisional — requires UAE SME
  validation*.

> Reminder: on Vercel, **uploaded documents are ephemeral** (`/tmp`); accounting data in
> Postgres persists. If a build fails on function size, trim `pdfplumber`/`xlrd` from
> `requirements.txt`.

---

## Alternative: Docker + persistent disk (Render / Railway / Fly) — simplest all-in-one

If you'd rather not manage a separate database, these hosts run the container 24/7 with a
mounted disk, so **SQLite + uploaded files both persist** with zero extra services. The
`Dockerfile`, `render.yaml` and `Procfile` are ready. See the Render/Railway/Fly steps above.
