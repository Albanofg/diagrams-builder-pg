"""PatentGeyser API: stateless, synchronous text -> PCT drawing pipeline.

POST /api/v1/generate  { text } -> { figures: [{ id, title, svgData, pdfBase64 }] }
GET  /health                    -> wake-up ping (Render cold-start mitigation)
"""

import hmac
import os
from pathlib import Path


def _load_dotenv() -> None:
    """Load backend/.env into the environment (no override of real env vars)."""
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware

import renderer
from llm_extractor import ExtractionError, extract_graph
from layout_engine import layout_figure
from models import GeneratedFigure, GenerateRequest, GenerateResponse
from renderer import render_figure

PARSE_ERROR = ("Could not resolve structural dependencies. "
               "Ensure text contains valid entity relationships.")

app = FastAPI(title="PatentGeyser API", version="1.0.0")

cors_origins = [
    origin.strip()
    for origin in os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared-secret gate for /api/v1/generate. When PATENTGEYSER_API_KEY is set, the
# caller must send it as the `X-API-Key` header. When it's unset the endpoint is
# left open (local dev) — with a loud startup warning so it's never shipped
# unauthenticated by accident.
API_KEY = os.environ.get("PATENTGEYSER_API_KEY", "").strip()

if not API_KEY:
    print(
        "WARNING: PATENTGEYSER_API_KEY is not set — /api/v1/generate is "
        "UNAUTHENTICATED. Set it in the environment (e.g. the Render dashboard) "
        "to require the X-API-Key header.",
        flush=True,
    )


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    if not API_KEY:
        return  # auth not configured — open (dev). Warned at startup.
    if not x_api_key or not hmac.compare_digest(x_api_key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/generate", response_model=GenerateResponse)
def generate(
    request: GenerateRequest,
    _: None = Depends(require_api_key),
) -> GenerateResponse:
    text = request.text.strip()
    if len(text) < 20:
        raise HTTPException(status_code=400, detail=PARSE_ERROR)

    try:
        graph = extract_graph(text)
    except ExtractionError as exc:
        print(f"EXTRACTION FAILED: {exc}", flush=True)
        raise HTTPException(status_code=400, detail=PARSE_ERROR)

    figures = []
    total = len(graph.figures)
    for figure in graph.figures:
        # Self-healing render: if the layout audit finds any text touching
        # a line or another text, re-lay the figure with wider spacing.
        svg = pdf = None
        for gap_boost in (1.0, 1.4, 1.9, 2.6):
            svg, pdf = render_figure(layout_figure(figure, gap_boost), total)
            if not renderer.LAST_AUDIT:
                break
        figures.append(GeneratedFigure(
            id=f"fig-{figure.figure_number}",
            title=figure.title,
            svgData=svg,
            pdfBase64=pdf,
        ))
    return GenerateResponse(figures=figures)
