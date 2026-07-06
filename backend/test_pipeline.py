"""QA harness: runs the full pipeline on the mocked AST (no OpenAI key needed).

    python test_pipeline.py

Writes qa_fig_N.svg / qa_fig_N.pdf next to this file and asserts the
deterministic and geometric guarantees the spec demands.
"""

import base64
import os
import sys

os.environ["MOCK_LLM"] = "1"

import renderer
from layout_engine import layout_figure
from llm_extractor import ensure_numerals, extract_graph
from renderer import render_figure


def main() -> int:
    graph = ensure_numerals(extract_graph("mock"))

    # ── numeral & identity discipline (rules: same element same numeral/
    # shape everywhere; numeral never reused; every block numbered;
    # ascending even sequence; stated numerals binding) ──
    id_to_numeral: dict = {}
    id_to_shape: dict = {}
    numeral_to_id: dict = {}
    assigned: list = []
    for figure in graph.figures:
        for node in figure.nodes:
            if node.shape in ("initial", "final"):
                assert not node.reference_numeral, \
                    "initial/final markers carry no numeral"
                continue
            assert node.reference_numeral, \
                f"every block must be numbered (missing: {node.id})"
            assert int(node.reference_numeral) % 2 == 0, \
                "numerals must be even"
            if node.id in id_to_numeral:
                assert id_to_numeral[node.id] == node.reference_numeral, \
                    f"same element {node.id} must keep one numeral"
                assert id_to_shape[node.id] == node.shape, \
                    f"same element {node.id} must keep one shape"
            else:
                id_to_numeral[node.id] = node.reference_numeral
                id_to_shape[node.id] = node.shape
                assigned.append(int(node.reference_numeral))
            owner = numeral_to_id.setdefault(node.reference_numeral, node.id)
            assert owner == node.id, \
                f"numeral {node.reference_numeral} labels two elements"
    assert id_to_numeral.get("processor") == "102", \
        "stated numeral 102 must be preserved"
    fresh = sorted(v for v in assigned if v != 102)
    assert fresh == sorted(set(fresh)) and all(
        v % 2 == 0 for v in fresh), "fresh numerals: one even ascending run"

    for figure in graph.figures:
        laid_a = layout_figure(figure)
        laid_b = layout_figure(figure)

        # determinism: identical input -> identical coordinates
        coords_a = [(n.id, n.x, n.y, n.w, n.h) for n in laid_a.nodes.values()]
        coords_b = [(n.id, n.x, n.y, n.w, n.h) for n in laid_b.nodes.values()]
        assert coords_a == coords_b, "layout must be deterministic"

        # canvas is content-sized; geometry must never be scaled/degenerate
        assert laid_a.width > 0 and laid_a.height > 0

        # no sibling overlaps (containers may enclose children)
        nodes = list(laid_a.nodes.values())
        for i, a in enumerate(nodes):
            for b in nodes[i + 1:]:
                if a.is_container or b.is_container:
                    continue
                overlap_x = min(a.x + a.w, b.x + b.w) - max(a.x, b.x)
                overlap_y = min(a.y + a.h, b.y + b.h) - max(a.y, b.y)
                assert not (overlap_x > 0.1 and overlap_y > 0.1), \
                    f"nodes {a.id} and {b.id} overlap"

        svg, pdf_b64 = render_figure(laid_a, len(graph.figures))
        assert not renderer.LAST_AUDIT, \
            f"layout audit violations: {renderer.LAST_AUDIT}"
        assert svg.lstrip().startswith("<?xml") and "</svg>" in svg
        pdf = base64.b64decode(pdf_b64)
        assert pdf.startswith(b"%PDF"), "PDF must be a valid PDF document"

        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, f"qa_fig_{figure.figure_number}.svg"),
                  "w", encoding="utf-8") as handle:
            handle.write(svg)
        with open(os.path.join(here, f"qa_fig_{figure.figure_number}.pdf"),
                  "wb") as handle:
            handle.write(pdf)
        print(f"FIG. {figure.figure_number} ({figure.figure_type}): "
              f"{len(laid_a.nodes)} nodes, {len(laid_a.edges)} edges, "
              f"{laid_a.width:.0f}x{laid_a.height:.0f}mm -> OK")

    print("All pipeline assertions passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
