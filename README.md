# Commitment Forms Extraction

## Running (step 1: upload + PDF-to-image + thumbnails)

Requirements: Python 3.11+, Node.js 20+.

**Backend** (http://localhost:8000, every route is under `/api`, e.g. http://localhost:8000/api/health):
```powershell
cd backend
python -m venv .venv                 # first time only
.\.venv\Scripts\activate
pip install -r requirements.txt      # first time only
uvicorn app.main:app --reload --port 8000
```

**Frontend** (http://localhost:3000):
```powershell
cd frontend
npm install                          # first time only
npm run dev
```

Open http://localhost:3000, drag PDF/image files onto the page. Originals and page images are stored under `uploads/<file_id>/` (git-ignored). Copy `.env.example` to `.env` for later steps (not needed yet).

## Deploying on Vercel (services)

`vercel.json` deploys the two folders as services of one project and one domain: `frontend/` (Next.js)
serves everything, and `/api/*` is routed to `backend/` (FastAPI, `app.main:app`). The backend receives
the path unchanged (`/api/upload`), which is why all its routes carry the `/api` prefix. The browser
calls the backend directly, on the same domain, so no service binding is needed. The frontend service
is built with `NEXT_PUBLIC_API_URL=/api`; under `vercel dev` set the same in `frontend/.env.local`.

Set in the Vercel project's environment variables (nothing is read from `.env` there):
`ANTHROPIC_API_KEY` (or `GOOGLE_API_KEY` / `MODEL_NAME`, whichever provider is active),
optionally `CLAUDE_MODEL`, `CONF_EMPTY_BELOW`, `CONF_OK_ABOVE`.

**Known limits of this setup, which the code does not solve:**
- A Vercel Function accepts a request body of at most **4.5 MB**, so uploading a PDF larger than that
  (the sample PDFs are about 13 MB) fails with 413.
- Uploaded files are kept on the function's local disk. On Vercel only `/tmp` is writable, it belongs to
  one instance and is short-lived, so a page uploaded in one request may be missing in the next one
  (`/api/extract` answers "page not found"). Real storage (e.g. Vercel Blob or S3) is needed.
- The app has no sign-in: anyone with the URL can upload forms. Turn on Deployment Protection first.
