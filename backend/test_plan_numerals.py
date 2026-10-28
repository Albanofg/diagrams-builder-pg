"""QA check: in plan mode the caller's ledger decides which element a numeral names.

    python test_plan_numerals.py

No model, no network. On 2026-10-07 a drafter citing a neighbour's numeral merged two
planned features whose words differ (402 and 502 on one of Tim's sheets), and the drawing
contradicted the text. A node whose words are a planned feature now carries that feature's
numeral; a wording the ledger does not hold, citing a planned numeral, is still read as
that feature; without a plan nothing changes.
"""

import sys

from llm_extractor import _harmonize, _identity_key
from models import Figure, Node, PatentGraph


def graph(*figs):
    return PatentGraph(
        figures=[
            Figure(
                figure_number=n,
                title=f"FIG {n}",
                figure_type="flowchart",
                nodes=[Node(id=f"n{n}{i}", label=label, reference_numeral=ref) for i, (label, ref) in enumerate(nodes)],
                edges=[],
            )
            for n, nodes in figs
        ]
    )


def numerals(g):
    return {(f.figure_number, n.label): n.reference_numeral for f in g.figures for n in f.nodes}


def planned_of(rows):
    return {_identity_key(feature, ""): ref for feature, ref in rows}


def main() -> int:
    ledger = [("flag store", "402"), ("flag store client", "502")]
    stated = {ref for _, ref in ledger}
    plan = planned_of(ledger)

    merged = numerals(_harmonize(graph((4, [("FLAG STORE", "402")]), (5, [("FLAG STORE CLIENT", "402")])), stated, plan))
    assert merged[(5, "FLAG STORE CLIENT")] == "502", "a planned feature keeps its own numeral, whatever its drafter cited"
    assert merged[(4, "FLAG STORE")] == "402", "...and the other keeps its own"

    first = numerals(_harmonize(graph((4, [("FLAG STORE CLIENT", "402")]), (5, [("FLAG STORE", "402")])), stated, plan))
    assert (first[(4, "FLAG STORE CLIENT")], first[(5, "FLAG STORE")]) == ("502", "402"), "the feature drawn first cannot take another's numeral"

    variant = numerals(_harmonize(graph((4, [("FLAG STORE CLIENT", "502")]), (5, [("FLAG STORE CLIENT MODULE", "502")])), stated, plan))
    assert variant[(5, "FLAG STORE CLIENT MODULE")] == "502", "an unplanned wording citing a planned numeral is that feature"

    acts = planned_of([("walk intervening flag values", "1206")])
    one_act = numerals(_harmonize(graph((12, [("WALK INTERVENING FLAG VALUES", "1206")]), (13, [("WALK THE INTERVENING FLAG VALUES", "1206")])), {"1206"}, acts))
    assert one_act[(13, "WALK THE INTERVENING FLAG VALUES")] == "1206", "one act drawn with and without its article stays one numeral"

    unplanned = numerals(_harmonize(graph((4, [("FLAG STORE", "402")]), (5, [("FLAG STORE CLIENT", "402")])), stated))
    assert unplanned[(5, "FLAG STORE CLIENT")] == "402", "without a plan the harmonizer reads as it did"

    # 2026-10-28 (the caller's plan 0037): one name planned as 108 on FIG. 1 and 308 on FIG. 3. The name's map kept only
    # 308, which FIG. 1 does not allow, so FIG. 1's box was drawn bare on every redraw.
    twice = [("selector index", "108"), ("selector index", "308")]
    all_refs = {}
    for feature, ref in twice:
        all_refs.setdefault(_identity_key(feature, ""), []).append(ref)
    sheets = {1: {"108"}, 3: {"308"}}
    per_sheet = numerals(_harmonize(graph((1, [("SELECTOR INDEX", "108")]), (3, [("SELECTOR INDEX", "308")])), {"108", "308"}, planned_of(twice), sheets, all_refs))
    assert (per_sheet[(1, "SELECTOR INDEX")], per_sheet[(3, "SELECTOR INDEX")]) == ("108", "308"), "a name planned once per sheet takes each sheet's own numeral"
    alone = numerals(_harmonize(graph((1, [("SELECTOR INDEX", "108")])), {"108", "308"}, planned_of(twice), {1: {"108"}}, all_refs))
    assert alone[(1, "SELECTOR INDEX")] == "108", "...a redraw of that one sheet included"

    print("All plan-numeral assertions passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
