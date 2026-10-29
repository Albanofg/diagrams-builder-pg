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

# S5 (2026-10-27, the caller's plan 0033 Part 5): a step number is never left alone on a box's last line.
from layout_engine import _keep_short_tail  # noqa: E402

check("S5: a short last line takes the word before it down, only when both fit; anything else stays as wrapped",
      (_keep_short_tail(["record the failure at step", "12"], 20), _keep_short_tail(["record the failure", "at step 12"], 20),
       _keep_short_tail(["record the failure at", "unrecoverableness", "12"], 12), _keep_short_tail(["12"], 20)),
      (["record the failure at", "step 12"], ["record the failure", "at step 12"], ["record the failure at", "unrecoverableness", "12"], ["12"]))

# B1 (2026-10-28, the caller's plan 0038): a sheet of any kind given as a graph is drawn as given,
# with no drafter call: a system sheet's components, store and outside source with the records
# that pass between them, and a records sheet's records with their fields and no line at all.
calls.clear()
llm_extractor._run_drafter = no_drafter
system = PlanFlow.model_validate({
    "nodes": [
        {"id": "c1", "label": "Citation Resolver", "numeral": "102", "kind": "component"},
        {"id": "c2", "label": "Release Step", "numeral": "104", "kind": "component"},
        {"id": "d1", "label": "Item Release", "numeral": "106", "kind": "datastore"},
        {"id": "x1", "label": "Normative Corpus", "numeral": "108", "kind": "external"},
    ],
    "edges": [
        {"from": "x1", "to": "c1", "label": "authority passages", "kind": "data"},
        {"from": "c1", "to": "c2", "label": "citation results", "kind": "data"},
        {"from": "c2", "to": "d1", "label": "release", "kind": "data"},
    ],
})
records = PlanFlow.model_validate({
    "nodes": [
        {"id": "r1", "label": "Draft", "numeral": "302", "kind": "entity", "fields": ["draft_id", "options"]},
        {"id": "r2", "label": "Item Provenance", "numeral": "304", "kind": "entity", "fields": ["cell", "grammar_version"]},
    ],
    "edges": [],
})
planned_b1 = [
    PlannedFigure(figNumber=1, figType="system", title="The system", outline="", numerals=["102", "104", "106", "108"]),
    PlannedFigure(figNumber=3, figType="record", title="The records", outline="", numerals=["302", "304"]),
]
ledger_b1 = [LedgerEntry(ref=r, feature=f) for r, f in [("102", "Citation Resolver"), ("104", "Release Step"), ("106", "Item Release"), ("108", "Normative Corpus"), ("302", "Draft"), ("304", "Item Provenance")]]
graph_b1 = draw_from_plan(planned_b1, ledger_b1, "", flows={1: system, 3: records}, allowed={1: {"102", "104", "106", "108"}, 3: {"302", "304"}})
by_fig = {f.figure_number: f for f in graph_b1.figures}
check("B1: no drafter call for a system or records sheet given as a graph", len(calls), 0)
check("B1: the system sheet's boxes, their numerals and shapes, as given",
      [(n.label, n.reference_numeral, n.shape) for n in by_fig[1].nodes],
      [("Citation Resolver", "102", "rectangle"), ("Release Step", "104", "rectangle"), ("Item Release", "106", "cylinder"), ("Normative Corpus", "108", "rectangle")])
ids_b1 = {n.id: n.label for n in by_fig[1].nodes}
check("B1: each record that passes between them is a line with its name",
      sorted((ids_b1[e.source_id], ids_b1[e.target_id], e.label) for e in by_fig[1].edges),
      sorted([("Normative Corpus", "Citation Resolver", "AUTHORITY PASSAGES"), ("Citation Resolver", "Release Step", "CITATION RESULTS"), ("Release Step", "Item Release", "RELEASE")]))
check("B1: a records sheet with no line is drawn, each record with its fields",
      [(n.label, n.reference_numeral, list(getattr(n, "fields", None) or [])) for n in by_fig[3].nodes],
      [("Draft", "302", ["draft_id", "options"]), ("Item Provenance", "304", ["cell", "grammar_version"])])
for f in graph_b1.figures:
    render_figure(layout_figure(f))
check("B1: both lay out and render", sorted(by_fig), [1, 3])
llm_extractor._run_drafter = original_drafter

if failures:
    print(f"\n{failures} check(s) failed")
    sys.exit(1)
print("\nAll plan-flow assertions passed.")
