# PatentGeyser Diagram API — Integration Guide

How to call the PatentGeyser diagram service from another application. You send
patent text; you get back a set of PCT-style figures as **SVG** (for display) and
**PDF** (for download/filing). The service is **stateless** — no sessions, no
database on the caller's side, one request in, figures out.

> **Audience:** developers of the *consuming* app (e.g. the Vercel frontend).
> **You do not need this repo's source to integrate** — only this document.

---

## 1. TL;DR

```
POST https://patentgeyser-api.onrender.com/api/v1/generate
Headers:
  Content-Type: application/json
  X-API-Key: <YOUR_API_KEY>
Body:
  { "text": "…patent claims / disclosure text…" }

200 OK →
  { "figures": [ { "id": "fig-1", "title": "…", "svgData": "<svg…>", "pdfBase64": "JVBERi0…" }, … ] }
```

- Authenticate with the `X-API-Key` header. **Call it from your server, never the browser** (the key must stay secret).
- `text` must be at least **20 characters** after trimming.
- Up to **8 figures** are returned per request.
- First call after idle can take **~50 s** (cold start — see §7). Use a **60 s timeout**.

---

## 2. Base URL & environment

| Environment | Base URL |
|---|---|
| Production (Render) | `https://patentgeyser-api.onrender.com` |
| Local dev | `http://localhost:8011` |

Store the base URL as configuration in the consuming app (e.g. `PATENTGEYSER_API_URL`)
rather than hard-coding it, so you can point at localhost during development.

---

## 3. Authentication

Every call to `/api/v1/generate` must include a shared secret in the **`X-API-Key`**
header. It is compared (constant-time) against the server's `PATENTGEYSER_API_KEY`.

```
X-API-Key: <YOUR_API_KEY>
```

- A **missing or wrong** key → `401 Unauthorized` (`{"detail":"Invalid or missing API key."}`).
- `GET /health` is **public** (no key required).

### Security rules (important)

- **Keep the key server-side only.** Do not embed it in client JavaScript, a mobile
  bundle, or any `NEXT_PUBLIC_*` variable — anything shipped to the browser is public.
- The correct pattern is a **server-to-server** call: your backend (or a Next.js Route
  Handler / Server Action) holds the key and calls this API. The browser talks only to
  *your* server. See §8.
- Rotate the key by changing `PATENTGEYSER_API_KEY` in the API's Render dashboard and
  updating the consuming app's env var.

---

## 4. Endpoints

### `GET /health`

Liveness probe. No auth. Use it to warm the service before a real call (§7).

```
200 OK
{ "status": "ok" }
```

### `POST /api/v1/generate`

Raw text → full pipeline (planner → drafter → layout → render). Auth required.
Synchronous — the response returns only when all figures are rendered. §5–§6.

### `POST /api/v1/draw`

**Plan mode.** For callers that run their own planner: send a *finished plan* and the
service **skips its planner**, running only drafter → layout → render. Auth required.
Full request/response contract in **§6a**.

---

## 5. Request

### Headers

| Header | Required | Value |
|---|---|---|
| `Content-Type` | yes | `application/json` |
| `X-API-Key` | yes | your shared secret |

### Body schema

```jsonc
{
  "text": "string"   // required. Raw patent claims / disclosure / specification text.
}
```

| Field | Type | Required | Constraints |
|---|---|---|---|
| `text` | string | yes | ≥ 20 characters after trimming; anything shorter → `400`. No hard upper limit, but longer text = more tokens = slower + higher LLM cost. |

- Any JSON field other than `text` is ignored.
- The body **must be valid JSON** with `text` as a JSON string. Malformed JSON → `422`.
- There is **no** `figureCount` / options parameter — the service decides how many
  figures the text warrants (capped at 8).

### Example

```json
{
  "text": "A system comprising a data ingestion module coupled to a compiler that extracts entities from source documents and generates a directed graph representing their relationships."
}
```

---

## 6. Response (`200 OK`)

```jsonc
{
  "figures": [
    {
      "id": "fig-1",                 // stable id, always "fig-<n>" (n = figure number)
      "title": "System Architecture Overview",
      "svgData": "<svg xmlns=…>…</svg>",   // raw SVG markup, UTF-8
      "pdfBase64": "JVBERi0xLjQ…"          // base64-encoded PDF bytes (NO data: prefix)
    }
    // …up to 8 figures
  ]
}
```

### Field reference

| Field | Type | Notes |
|---|---|---|
| `figures` | array | 1–8 items. Never empty on `200` (if nothing could be drawn you get a `400`, not an empty array). Order corresponds to figure number. |
| `figures[].id` | string | `"fig-1"`, `"fig-2"`, … Use as a stable React key. |
| `figures[].title` | string | Human-readable figure caption. |
| `figures[].svgData` | string | **Raw SVG document** (`<svg …>…</svg>`). Inject directly into the DOM or write to a `.svg` file. Self-contained, black-and-white, PCT-style. |
| `figures[].pdfBase64` | string | **Bare base64** of a single-page A4 PDF. To use it, prepend `data:application/pdf;base64,`. Not a data URI by itself. |

### Rendering the output (consuming app)

**Display the SVG** — it's a complete SVG document, so you can inject it:

```tsx
// React — render the SVG markup returned by the API
function Figure({ fig }: { fig: { id: string; title: string; svgData: string } }) {
  return (
    <figure>
      <div dangerouslySetInnerHTML={{ __html: fig.svgData }} />
      <figcaption>{fig.title}</figcaption>
    </figure>
  );
}
```

> The SVG is generated by this service (not arbitrary user HTML), but it *is* injected
> markup — keep it flowing straight from the API to `dangerouslySetInnerHTML` and don't
> mix in untrusted content.

**Offer the PDF as a download** — prepend the data-URI prefix:

```tsx
<a
  href={`data:application/pdf;base64,${fig.pdfBase64}`}
  download={`${fig.id}.pdf`}
>
  Download {fig.title} (PDF)
</a>
```

**Decode the PDF on a server (Node):**

```ts
const pdfBytes = Buffer.from(fig.pdfBase64, "base64");
// fs.writeFileSync(`${fig.id}.pdf`, pdfBytes)
```

---

## 6a. Plan mode — `POST /api/v1/draw` (skip the planner)

For callers that already run their **own** planner (e.g. app-2, which holds the full
disclosure and its own LLM stack), this endpoint accepts a **finished plan** and runs
only `drafter → layout → render` — the service's planner LLM pass is skipped. Less time
per request and bounded input. `/generate` (raw text → everything) stays available and
unchanged.

> **Source of truth for the plan schema:** `DIAGRAM_SERVICE_PLAN_MODE.md` in the
> consuming app's ("app-2") repo. That file defines the plan the caller's planner
> produces; this section mirrors it. If it changes, this section follows.

Auth and headers are **identical to `/generate`** (`X-API-Key`, `Content-Type: application/json`).

### Request body

```jsonc
{
  "plan": {
    "figures": [
      {
        "figNumber": 1,                    // 1-based; echoed back for matching
        "figType": "system",              // system|module|flowchart|dataflow|sequence|state|hardware|record
        "title": "System Architecture Overview",
        "outline": "Draw DEVICE (100) as a container. Inside it: CAPTURE INTERFACE (102) coupled to TRIGGER DETECTOR (104); … — every element with its numeral + catchword, every connection and its nature, every containment, in drawing order.",
        "numerals": ["100", "102", "104"]  // the numerals THIS figure may use
      }
      // …up to 8 figures (MAX_FIGURES)
    ],
    "numerals": [                          // global ledger: ref -> canonical feature
      { "ref": "100", "feature": "Device", "figures": [1], "definedInSpec": false },
      { "ref": "102", "feature": "Capture Interface", "figures": [1, 2], "definedInSpec": true }
    ]
  },
  "spec": "optional short context string"  // OPTIONAL — the outline is authoritative; usually omit
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `plan.figures[]` | array | yes | One entry per figure to draw — each is a ready assignment for the drafter. |
| `plan.figures[].figNumber` | int | yes | 1-based. Echoed back in the response for matching. |
| `plan.figures[].figType` | enum | yes | One of the 8 values above. Anything else → `422`. |
| `plan.figures[].title` | string | yes | Used verbatim as the figure title. |
| `plan.figures[].outline` | string | yes | **Authoritative** drawing instructions — names every element (numeral + catchword), every connection and its nature, and every containment, in drawing order. |
| `plan.figures[].numerals[]` | string[] | yes | The numerals this figure is allowed to use. |
| `plan.numerals[]` | array | yes | Global numeral **ledger**: `ref → feature`, with `figures[]` and `definedInSpec`. |
| `spec` | string | no | Optional fallback context. The outline stands alone when omitted. |

- Extra fields the caller carries on a figure (`briefDescription`, `detailedDescription`,
  `illustrates`, …) are **ignored** — send them freely.
- `plan.figures` empty or malformed → `400`. Figures beyond 8 are dropped (`MAX_FIGURES`).

### Response (`200 OK`)

Same shape as `/generate`, **plus `figNumber` and `numerals`** per figure:

```jsonc
{
  "figures": [
    {
      "figNumber": 1,                    // echoes plan.figures[].figNumber
      "id": "fig-1",
      "title": "System Architecture Overview",
      "svgData": "<svg …>…</svg>",       // raw SVG markup (same as /generate)
      "pdfBase64": "JVBERi0…",           // bare base64 PDF (same as /generate)
      "numerals": ["100", "102", "104"]  // the numerals ACTUALLY drawn
    }
    // …one per figure successfully drawn, in figNumber order
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `figNumber` | int | The plan's `figNumber` for this figure. Join the returned svg/pdf back to its plan entry (and its description). |
| `numerals` | string[] | The numerals the drawing **actually placed** (after the drafter's legibility budget). Reconcile your stored description against these so text and drawing never disagree. |
| `id`, `title`, `svgData`, `pdfBase64` | — | Same as `/generate` (§6). |

- **The ledger is law.** The numerals you send are preserved as-is — the service does
  **not** renumber them. `numerals` reflects exactly what was drawn.
- **Partial success:** a per-figure drafter failure drops that one figure and keeps the
  rest, so you may receive **fewer figures than planned**. Match by `figNumber` and treat
  a missing number as a figure to retry.
- Everything else — auth, `/health`, `MAX_FIGURES`, timeouts (§7), error shapes (§10) —
  is identical to `/generate`.

### Example call (server-side)

```ts
const r = await fetch(`${API_URL}/api/v1/draw`, {
  method: "POST",
  headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
  body: JSON.stringify({ plan }), // plan = your planner's output (see schema above)
  signal: AbortSignal.timeout(60_000),
});
if (!r.ok) throw new Error((await r.json()).detail ?? "draw failed");
const { figures } = await r.json();
// figures: [{ figNumber, id, title, svgData, pdfBase64, numerals }]
```

---

## 7. Timeouts, cold starts & retries

The API is hosted on Render's **free tier**, which **spins the service down after
inactivity**. Consequences for the caller:

- The **first request after idle** pays a cold start: the container boots, then your
  request runs. Budget **up to ~50–60 seconds** for that first call.
- A warm call (LLM + render) typically completes in a few seconds, but depends on text
  length and OpenAI latency.

**Recommendations:**

1. **Set a generous client timeout — at least 60 s** on the `/api/v1/generate` call.
   (On Vercel, also raise the function's `maxDuration`; see §8.)
2. **Warm it up first** if you can: fire `GET /health` when the user lands on the page /
   opens the editor, so the container is awake by the time they submit.
3. **Optional keep-warm:** a cron (GitHub Actions, cron-job.org, etc.) pinging `/health`
   every ~10 minutes keeps cold starts away entirely. Or upgrade the Render plan.
4. **Retry** transient network/timeout errors once or twice with backoff. Do **not**
   auto-retry `400`/`401`/`422` — those are deterministic and won't change on retry.

---

## 8. Recommended integration — Next.js (Vercel), server-side

Your browser calls **your own** route; that route holds the secret and calls PatentGeyser.
The key never reaches the client.

### App Router — `app/api/diagrams/route.ts`

```ts
import { NextResponse } from "next/server";

export const runtime = "nodejs";
export const maxDuration = 60; // allow for Render cold starts

const API_URL = process.env.PATENTGEYSER_API_URL; // https://patentgeyser-api.onrender.com
const API_KEY = process.env.PATENTGEYSER_API_KEY; // the X-API-Key secret

export async function POST(req: Request) {
  if (!API_URL || !API_KEY) {
    return NextResponse.json({ error: "Diagram API not configured" }, { status: 500 });
  }

  let text: unknown;
  try {
    text = (await req.json())?.text;
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  if (typeof text !== "string" || text.trim().length < 20) {
    return NextResponse.json({ error: "Provide 'text' of at least 20 characters" }, { status: 400 });
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${API_URL}/api/v1/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
      body: JSON.stringify({ text }),
      signal: AbortSignal.timeout(60_000), // cold-start safe
    });
  } catch {
    return NextResponse.json({ error: "Diagram service unreachable or timed out" }, { status: 504 });
  }

  const data = await upstream.json().catch(() => null);
  if (!upstream.ok) {
    // Forward a useful message; `detail` is the API's error field
    const message = typeof data?.detail === "string" ? data.detail : "Diagram generation failed";
    return NextResponse.json({ error: message }, { status: upstream.status });
  }

  return NextResponse.json(data); // { figures: [...] }
}
```

### Pages Router — `pages/api/diagrams.ts`

```ts
import type { NextApiRequest, NextApiResponse } from "next";

export const config = { maxDuration: 60 };

export default async function handler(req: NextApiRequest, res: NextApiResponse) {
  if (req.method !== "POST") return res.status(405).json({ error: "Method not allowed" });

  const API_URL = process.env.PATENTGEYSER_API_URL;
  const API_KEY = process.env.PATENTGEYSER_API_KEY;
  if (!API_URL || !API_KEY) return res.status(500).json({ error: "Diagram API not configured" });

  const text = req.body?.text;
  if (typeof text !== "string" || text.trim().length < 20) {
    return res.status(400).json({ error: "Provide 'text' of at least 20 characters" });
  }

  try {
    const upstream = await fetch(`${API_URL}/api/v1/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
      body: JSON.stringify({ text }),
      signal: AbortSignal.timeout(60_000),
    });
    const data = await upstream.json().catch(() => null);
    if (!upstream.ok) {
      const message = typeof data?.detail === "string" ? data.detail : "Diagram generation failed";
      return res.status(upstream.status).json({ error: message });
    }
    return res.status(200).json(data);
  } catch {
    return res.status(504).json({ error: "Diagram service unreachable or timed out" });
  }
}
```

### Browser side — call your own route (no key here)

```ts
const res = await fetch("/api/diagrams", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ text }),
});
if (!res.ok) throw new Error((await res.json()).error);
const { figures } = await res.json();
// figures.forEach(render…)
```

### Consuming-app environment variables (Vercel → Settings → Environment Variables)

| Variable | Example | Scope |
|---|---|---|
| `PATENTGEYSER_API_URL` | `https://patentgeyser-api.onrender.com` | Server (not `NEXT_PUBLIC_`) |
| `PATENTGEYSER_API_KEY` | `<the shared secret>` | Server (not `NEXT_PUBLIC_`) |

---

## 9. Calling from a non-Next.js backend

Any server that can make an HTTPS POST works. Example with `fetch` (Node 18+):

```ts
const r = await fetch("https://patentgeyser-api.onrender.com/api/v1/generate", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-API-Key": process.env.PATENTGEYSER_API_KEY!,
  },
  body: JSON.stringify({ text }),
  signal: AbortSignal.timeout(60_000),
});
if (!r.ok) throw new Error(`API ${r.status}: ${(await r.json()).detail ?? "error"}`);
const { figures } = await r.json();
```

`curl` (bash):

```bash
curl -sS -X POST "https://patentgeyser-api.onrender.com/api/v1/generate" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $PATENTGEYSER_API_KEY" \
  -d '{"text":"A system comprising a data ingestion module coupled to a compiler…"}'
```

PowerShell (Windows) — use `Invoke-RestMethod`, not the `curl` alias:

```powershell
$body = '{"text":"A system comprising a data ingestion module coupled to a compiler…"}'
Invoke-RestMethod -Method Post `
  -Uri "https://patentgeyser-api.onrender.com/api/v1/generate" `
  -Headers @{ "X-API-Key" = $env:PATENTGEYSER_API_KEY } `
  -ContentType "application/json" -Body $body
```

---

## 10. Error responses

All errors return JSON. FastAPI puts the message in a **`detail`** field (a string for
app errors, an array for validation errors).

| Status | Meaning | Example body | How to handle |
|---|---|---|---|
| `400 Bad Request` | `text` shorter than 20 chars, **or** the text couldn't be resolved into any drawable figure. | `{"detail":"Could not resolve structural dependencies. Ensure text contains valid entity relationships."}` | Show the user a "couldn't generate a diagram from this text" message. Don't retry unchanged. |
| `401 Unauthorized` | Missing/incorrect `X-API-Key`. | `{"detail":"Invalid or missing API key."}` | Fix the key on the caller. Not user-facing. |
| `422 Unprocessable Entity` | Body isn't valid JSON, or `text` field missing/not a string. | `{"detail":[{"type":"json_invalid","loc":["body",1],"msg":"JSON decode error",…}]}` | Caller bug — ensure `Content-Type: application/json` and a proper JSON string body. |
| `500 Internal Server Error` | Server-side failure (e.g. the LLM key isn't configured on the server, or a render error). | `{"detail":"…"}` | Not the caller's fault; retry once, then surface a generic error. Check the API's server logs. |
| `502 / 503 / timeout` | Service asleep/booting (cold start) or restarting. | (may be a Render HTML page, not JSON) | Retry with backoff after warming via `/health`. |

**Parsing tip:** always guard `await res.json()` — cold-start/proxy errors can return
non-JSON. In the examples above, `.catch(() => null)` handles that.

---

## 11. Behavior notes & limits

- **Figures per request:** capped at **8** (`MAX_FIGURES`). Longer specs are not split
  into more figures beyond that.
- **Determinism:** output is LLM-generated and will vary slightly between identical
  requests. Don't rely on exact node/edge layout being stable.
- **Payload size:** each figure's `svgData` is typically ~8–15 KB and `pdfBase64`
  ~20–40 KB. An 8-figure response can be a few hundred KB of JSON — fine over HTTP, but
  don't log full responses.
- **Statelessness:** the API stores nothing. If you need history/persistence, store the
  returned `figures` in the consuming app's own database.
- **Concurrency:** requests are independent; you can fan out multiple in parallel
  (subject to the API's own OpenAI rate limits).

---

## 12. End-to-end flow (summary)

```
User (browser)
   │  POST /api/diagrams  { text }          ← your Vercel route (holds the secret)
   ▼
Your Next.js Route Handler
   │  POST /api/v1/generate  { text }
   │  header X-API-Key: <secret>            ← server-to-server, key never in browser
   ▼
PatentGeyser API (Render)
   │  OpenAI plan → draft → Matplotlib render
   ▼
   {figures:[{id,title,svgData,pdfBase64}]} → your route → browser → render SVG / offer PDF
```

---

*Contract source of truth: `backend/models.py` (`GenerateRequest`, `GeneratedFigure`,
`GenerateResponse`) and `backend/main.py` in this repo. If those change, update this file.*
