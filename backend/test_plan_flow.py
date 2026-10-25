"""QA check (2026-10-24, S1 to S3): a planned flowchart is drawn as planned, each sheet keeps
its own plan's numerals, and a failed drafter call is tried once more.

    python test_plan_flow.py

No model, no network. On the caller's 22 graded briefs, 15 of 57 blockers were a drawing
against the text: the drafter rebuilt every flowchart line from the outline's words, the
numbering pass kept any ledger numeral a box cited, and a failed drafter call dropped its
sheet in silence.
"""

import sys

import httpx
from openai import APIError

import llm_extractor
from llm_extractor import _harmonize, draw_from_plan
from models import Figure, LedgerEntry, Node, PatentGraph, PlanFlow, PlannedFigure

failures = 0


def check(name, got, want):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" (got {got!r}, want {want!r})"))


# -- S2: a planned flowchart is drawn as given, without the drafter -----------------
original_drafter = llm_extractor._run_drafter
calls = []


def no_drafter(*args, **kwargs):
    calls.append(1)
    return None


llm_extractor._run_drafter = no_drafter
flow = PlanFlow.model_validate({
    "nodes": [
        {"id": "n1", "label": "Start", "kind": "start"},
        {"id": "n2", "label": "resolve option citations", "numeral": "402", "kind": "process"},
        {"id": "n3", "label": "Optionless answer supported?", "numeral": "412", "kind": "decision"},
        {"id": "n4", "label": "A", "kind": "connector"},
        {"id": "n5", "label": "withhold the draft", "numeral": "414", "kind": "process"},
        {"id": "n6", "label": "End", "kind": "end"},
    ],
    "edges": [
        {"from": "n1", "to": "n2"},
        {"from": "n2", "to": "n3"},
        {"from": "n3", "to": "n4", "label": "yes"},
        {"from": "n3", "to": "n5", "label": "unknown"},
        {"from": "n5", "to": "n6"},
        {"from": "n5", "to": "n9"},
    ],
})
planned = [PlannedFigure(figNumber=4, figType="flowchart", title="Answer before options",
                         outline="Start -> resolve option citations -> ...", numerals=["402", "412", "414"])]
ledger = [
    LedgerEntry(ref="402", feature="resolve option citations"),
    LedgerEntry(ref="412", feature="Optionless answer supported?"),
    LedgerEntry(ref="414", feature="withhold the draft"),
    LedgerEntry(ref="100", feature="System Environment"),
]
graph = draw_from_plan(planned, ledger, "", flows={4: flow}, allowed={4: {"402", "412", "414"}})
fig = graph.figures[0]
check("S2: no drafter call for a planned flowchart", len(calls), 0)
check("S2: every planned box is drawn, by its label",
      [n.label for n in fig.nodes],
      ["Start", "resolve option citations", "Optionless answer supported?", "A", "withhold the draft", "End"])
by_id = {n.id: n.label for n in fig.nodes}
check("S2: every line to a planned box is drawn with its answer; a line to a box never planned is not",
      sorted((by_id[e.source_id], by_id[e.target_id], e.label) for e in fig.edges),
      sorted([
          ("Start", "resolve option citations", None),
          ("resolve option citations", "Optionless answer supported?", None),
          ("Optionless answer supported?", "A", "YES"),
          ("Optionless answer supported?", "withhold the draft", "UNKNOWN"),
          ("withhold the draft", "End", None),
      ]))
check("S2: the decision is drawn as a diamond", next(n.shape for n in fig.nodes if n.label.endswith("?")), "diamond")
check("S1: the planned boxes carry their numerals; the bookends and the connector carry none",
      {n.label: n.reference_numeral for n in fig.nodes},
      {"Start": None, "resolve option citations": "402", "Optionless answer supported?": "412",
       "A": None, "withhold the draft": "414", "End": None})
llm_extractor._run_drafter = original_drafter

# The planned flowchart lays out and renders as any drafted one does, its text clear of its lines.
import renderer
from layout_engine import layout_figure
from renderer import render_figure

svg = pdf = None
for gap_boost in (1.0, 1.4, 1.9, 2.6):
    svg, pdf = render_figure(layout_figure(fig, gap_boost), 1)
    if not renderer.LAST_AUDIT:
        break
check("S2: the planned flowchart renders, with a PDF, and its layout audit clean",
      (bool(svg and "<svg" in svg), bool(pdf), not renderer.LAST_AUDIT), (True, True, True))
check("S2: its answers are printed on the sheet", all(word in svg for word in ("YES", "UNKNOWN")), True)

# -- S1: a sheet carries only its own plan's numerals ---------------------------------
two = PatentGraph(figures=[
    Figure(figure_number=1, title="FIG 1", figure_type="block_diagram", nodes=[
        Node(id="a", label="HOUSEHOLD LEDGER", reference_numeral="100"),
        Node(id="b", label="SUBMITTED REQUESTS"),
        Node(id="c", label="ALLOCATION ENGINE", reference_numeral="104"),
    ], edges=[]),
])
stated = {"100", "104"}
plan_keys = {"SYSTEM ENVIRONMENT": "100", "ALLOCATION ENGINE": "104"}
kept = _harmonize(two.model_copy(deep=True), stated, plan_keys, {1: {"104"}})
check("S1: a ledger numeral the sheet's plan does not list is dropped; a box no feature names gets none",
      {n.label: n.reference_numeral for n in kept.figures[0].nodes},
      {"HOUSEHOLD LEDGER": None, "SUBMITTED REQUESTS": None, "ALLOCATION ENGINE": "104"})
free = _harmonize(two.model_copy(deep=True), stated, plan_keys)
check("S1: without a plan's numerals per sheet, nothing changes (every box numbered as before)",
      all(n.reference_numeral for n in free.figures[0].nodes), True)

# -- S3: a failed drafter call is tried once more -------------------------------------
attempts = []


class FakeResponses:
    def parse(self, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise APIError("boom", request=httpx.Request("POST", "https://example.invalid"), body=None)
        return type("R", (), {"output_parsed": "drafted"})()


class FakeClient:
    responses = FakeResponses()


llm_extractor._client = lambda: FakeClient()
got = llm_extractor._run_drafter(planned[0], "{}", "")
check("S3: a failed drafter call is tried once more, and its answer kept", (len(attempts), got), (2, "drafted"))
attempts.clear()


class AlwaysFails:
    def parse(self, **kwargs):
        attempts.append(1)
        raise APIError("boom", request=httpx.Request("POST", "https://example.invalid"), body=None)


llm_extractor._client = lambda: type("C", (), {"responses": AlwaysFails()})()
check("S3: two failures drop the sheet, never a third call", (llm_extractor._run_drafter(planned[0], "{}", ""), len(attempts)), (None, 2))

# -- S4: a redraw keeps the sheet as last drawn -----------------------------------------
seen_prompts = []


class Remembers:
    def parse(self, **kwargs):
        seen_prompts.append(kwargs.get("input", ""))
        return type("R", (), {"output_parsed": None})()


llm_extractor._client = lambda: type("C", (), {"responses": Remembers()})()
last = {"figNumber": 2, "figType": "system", "title": "T", "nodes": [{"id": "gate", "type": "component", "label": "RELEASE GATE", "ref": "116"}], "edges": []}
llm_extractor._run_drafter(planned[0], "{}", "", last)
llm_extractor._run_drafter(planned[0], "{}", "")
check("S4: a redraw's drafter is given the sheet as last drawn; a first draw is given none",
      ('"RELEASE GATE"' in seen_prompts[0], "[PREVIOUS_SHEET_JSON]" in seen_prompts[0], '"RELEASE GATE"' in seen_prompts[1], "[PREVIOUS_SHEET_JSON]" in seen_prompts[1]),
      (True, False, False, False))
llm_extractor._run_drafter = no_drafter
drafts = {}
draw_from_plan(planned, ledger, "", flows={4: flow}, allowed={4: {"402", "412", "414"}}, drafts_out=drafts)
check("S4: each drawn sheet's draft comes back, by its number, for the caller to send next time",
      (sorted(drafts), [n["label"] for n in drafts[4]["nodes"]][:2], "from" in drafts[4]["edges"][0]),
      ([4], ["Start", "resolve option citations"], True))
llm_extractor._run_drafter = original_drafter

if failures:
    print(f"\n{failures} check(s) failed")
    sys.exit(1)
print("\nAll plan-flow assertions passed.")
