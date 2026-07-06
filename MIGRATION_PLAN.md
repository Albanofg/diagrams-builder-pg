# Migration Plan — Replace Eraser with the In-House PCT Engine

**Goal:** Stop using Eraser for the "Technical Diagrams" step in Patent Geyser.
Instead, send the full disclosure to your own engine, which plans and draws the
whole figure set itself and returns PCT-compliant, filing-ready drawings
(SVG + PDF). This removes per-diagram Eraser cost and gives drawings a user can
actually take into a real patent.

---

## Glossary (so names stay straight)

- **Main app** — Patent Geyser, the customer-facing product on Vercel. Has its
  own planner; today it POSTs one figure description per diagram to Eraser.
- **Engine** — the new `patentgeyser` repo. Takes the *whole* disclosure text,
  runs its own planner + drafter + deterministic layout + renderer, and returns
  the entire figure set in one response.

---

## Architecture (locked)

One call replaces the per-diagram loop:

```
Main app (Vercel)
   │  POST /api/v1/generate  { text: <full disclosure> }      ← ONE call, not N
   ▼
Engine (hosted)
   plans figures → drafts each → lays out → renders
   │  ← { figures: [ { id, title, svgData, pdfBase64 }, ... ] }
   ▼
Main app renders the cards from that array
```

Why one call, not per-figure: the engine's value is a single numeral ledger
(consistent reference numbers across all figures) — a core PCT requirement. That
only works when the engine sees the whole disclosure at once.

---

## Decisions to confirm before building

1. **Retire the main app's diagram planner.** Full-handoff means the engine
   decides the figure set, so your planner no longer drives the diagram step.
   Keep it for the provisional-draft text if you use it there; it just stops
   feeding diagrams. (Confirm: OK to retire for diagrams.)
2. **Dynamic figure count.** Engine returns 1–8 figures, not a fixed 5. The
   cards must render however many come back. (Confirm: UI handles a variable
   count.)
3. **"Regenerate" button meaning.** The engine is deterministic — same text →
   identical drawings. Per-card regenerate doesn't really exist. Options: hide
   per-card regenerate, or make it "edit text → regenerate the whole set."
   (Confirm which.)
4. **Privacy note (not a blocker).** Today only short figure descriptions go to
   Eraser. The new flow sends the full pre-filing disclosure to your engine and
   to OpenAI. OpenAI's API doesn't train on it by default, but it's a third
   party seeing unfiled invention text. (Confirm: acceptable.)

---

## Phases

### Phase 1 — Deploy the engine
- Host on **Render free tier** to start (the repo already has `render.yaml`; $0,
  no card). Trade-off: sleeps after ~15 min idle, so the first generation of the
  day takes ~30–50s to wake.
- Set env vars: `OPENAI_API_KEY`, `OPENAI_MODEL`, and `CORS_ORIGINS` = the main
  app's Vercel domain.
- Result: a live `https://...onrender.com` URL.
- Done when: hitting `/health` returns OK and a manual `/api/v1/generate` test
  with sample text returns figures.

### Phase 2 — Swap the Eraser call in the main app
- Find the code that loops over planned figures and POSTs to
  `https://app.eraser.io/api/render/prompt`.
- Replace the whole loop with a single POST of the full disclosure text to the
  engine URL.
- Map the response: each `figure.svgData` → on-screen display; each
  `figure.pdfBase64` → the per-figure download and the "Download Blueprint".
- Remove the Eraser type-mapping and the `diagramType/mode/theme` payload — the
  engine picks types itself.

### Phase 3 — Reconcile the UI
- Render a dynamic number of diagram cards.
- Apply the "Regenerate" decision from above.
- Confirm "Download the Invention Concept Blueprint" now bundles the engine's
  PDFs (the filing-ready artifact — the whole point of this migration).

### Phase 4 — Test on staging
- Point a staging copy of the main app at the engine URL.
- Run a real provisional draft through it; eyeball the SVGs and confirm the PDFs
  are valid A4 PCT figures. (`backend/test_pipeline.py` already asserts
  determinism, page-fit, and no overlaps.)

### Phase 5 — Cutover
- Flip the production main app to the engine URL.
- Keep Eraser credentials/flag for one cycle as an instant fallback.
- Remove Eraser once stable.

### Phase 6 — Watch cost, scale only if needed
- Hosting stays ~$0 on Render free.
- Real variable cost is now **OpenAI**: each generation = 1 planner call + up to
  ~8 drafter calls (run in parallel). Track this per generation.
- Upgrade the host (Render $7 always-on, or Cloud Run free tier) only when cold
  starts actually bother a customer — not before.

---

## Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Cold start makes first daily generation slow | Main app already pings `/health` on load to pre-warm; upgrade host later if it bites |
| Engine returns a different figure set than customers expect | Review on staging with real drafts before cutover |
| OpenAI cost creeps up with volume | Watch per-generation call count; cap `MAX_FIGURES` if needed |
| Engine down / errors | Keep Eraser fallback enabled for one cycle |

---

## What's needed to move from plan to build

1. Add the **main app's repo** (the Vercel one) to the workspace so the exact
   Eraser-call code can be located and rewritten.
2. Confirm **Render free** as the starting host.
3. Confirm the four decisions above.
