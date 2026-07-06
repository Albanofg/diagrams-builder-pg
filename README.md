# PatentGeyser

Translate unstructured patent claims into deterministic, PCT-compliant vector
drawings in seconds. A precision drafting tool — not a generic AI image
generator.

**Stack (latest as of June 2026):** Next.js 16.2 + React 19.2 + Tailwind v4
(frontend) · FastAPI + Matplotlib + OpenAI `gpt-5.4-mini` Structured Outputs
(backend).

## Architecture

```
React UI (Vercel)
   │  POST /api/v1/generate { text }
   ▼
FastAPI (Render)
   1. llm_extractor.py  — gpt-5.4-mini parses text → PatentGraph AST (strict schema)
   2. layout_engine.py  — deterministic hierarchical / packing layout (no random seeds)
   3. renderer.py       — Matplotlib draws PCT-compliant primitives, orthogonal routing
   │  ← { figures: [{ id, title, svgData, pdfBase64 }] }
   ▼
Multi-sheet viewer: FIG. 1..N tabs, pan/zoom, SVG + PDF export
```

Ephemeral and private: zero login, zero database — close the tab and the data
is gone.

## Local development

Backend (Python 3.13+):

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
$env:OPENAI_API_KEY = "sk-..."          # or  $env:MOCK_LLM = "1"  for no-key dev
.venv\Scripts\python -m uvicorn main:app --port 8000
```

Frontend (Node 20.9+):

```powershell
cd frontend
npm install
npm run dev                              # http://localhost:3000
```

`frontend/.env.local` sets `NEXT_PUBLIC_API_BASE` (default
`http://localhost:8011`; change to wherever the backend runs — it is inlined
at build time).

### Tests / QA

- `backend: .venv\Scripts\python test_pipeline.py` — full pipeline on a mocked
  AST (no key needed); asserts determinism, page fit, no overlaps; writes
  `qa_fig_N.svg/pdf`.
- `qa: npm install && node verify.mjs` — Playwright (system Edge) drives the
  real UI end-to-end against a running backend+frontend.

## Environment variables

| Where    | Variable               | Purpose                                        |
| -------- | ---------------------- | ---------------------------------------------- |
| backend  | `OPENAI_API_KEY`       | NLP extraction (required unless `MOCK_LLM=1`)  |
| backend  | `OPENAI_MODEL`         | default `gpt-5.4-mini`                         |
| backend  | `CORS_ORIGINS`         | comma-separated allowed origins                |
| backend  | `MOCK_LLM`             | `1` = deterministic sample AST, no API calls   |
| frontend | `NEXT_PUBLIC_API_BASE` | backend URL (build-time)                       |

## Deployment

1. **Backend → Render:** connect the repo, Render reads `render.yaml`
   (rootDir `backend`, uvicorn on port 10000). Set `OPENAI_API_KEY` and
   `CORS_ORIGINS` (your Vercel URL) in the dashboard. Free tier spins down
   after 15 min idle → first request of the day may take 30–50 s; the
   frontend pings `/health` on page load to start the wake-up early.
2. **Frontend → Vercel:** import the repo, set the project **Root Directory**
   to `frontend`, framework preset Next.js. Add env var
   `NEXT_PUBLIC_API_BASE` = your Render URL.

## PCT compliance notes

- A4 sheet (210×297 mm), usable area 170×262 mm (25 mm top/left, 15 mm right,
  10 mm bottom margins).
- Single uniform 1.5 pt (~0.5 mm) black stroke, no fills, no color.
- Orthogonal edge routing only (L-elbows and Z-routes, crossing-aware).
- Reference numerals italic at the top-right of each element; stated numerals
  (e.g. "processor (102)") are preserved exactly, missing ones are assigned
  sequentially (100, 102, 104…) and stay synchronized across figures.
