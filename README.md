# Commitment Forms Extraction

## Running (step 1: upload + PDF-to-image + thumbnails)

Requirements: Python 3.11+, Node.js 20+.

**Backend** (http://localhost:8000):
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
