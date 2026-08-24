"""Render the layer diagram to `docs/img/architecture.png`.

    python -m scripts.make_architecture_diagram

PLAN.md M9 asks for the architecture diagram to be exported. Drawing it in code rather
than in a diagramming tool means it lives in the repository, diffs like everything else,
and cannot quietly drift from the package layout — `test_the_diagram_matches_the_package`
in `tests/unit/test_console_contract.py`'s neighbourhood checks that every box names a
directory that exists.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

import antar

# (title, module path, one-line job, colour)
LAYERS: list[tuple[str, str, str, str]] = [
    (
        "L1  signals",
        "antar/signals/",
        "Webhooks, downtime feed, error taxonomy.  ->  AtRiskEvent",
        "#dbe7f3",
    ),
    (
        "L2  detect",
        "antar/detect/",
        "WHY did it fail? Six classes, cost-weighted.  ->  Diagnosis",
        "#d7ecdf",
    ),
    (
        "L3  decide",
        "antar/decide/",
        "WHO benefits, and is it worth the harm? CATE -> LP.  ->  Decision",
        "#fdeacd",
    ),
    (
        "PolicyGate",
        "antar/policy/gate.py",
        "Caps - idempotency - dry-run - human approval.  No bypass path.",
        "#f6d6d4",
    ),
    (
        "L4  act",
        "antar/act/",
        "Registered templates. The LLM fills ONE slot.  ->  ActionRecord",
        "#e6dcf0",
    ),
    (
        "L5  audit",
        "antar/audit/",
        "Append-only, hash-chained. trace - replay.  The durable truth",
        "#e3e3e3",
    ),
]

CAPTION = (
    "Money flows down. Nothing flows back up: audit reads from every layer and none "
    "reads from it.\n"
    "The gate is a layer, not a decorator - every money action passes through it, and a "
    "test discovers the executors rather than listing them (N2)."
)


def draw() -> Path:
    fig, ax = plt.subplots(figsize=(11, 8.5))
    ax.set_xlim(0, 10)
    # Headroom computed from the stack rather than guessed: the top box ends at
    # 0.9 + (n-1)*(height+gap) + height, and the title needs clear air above it.
    top_of_stack = 0.9 + (len(LAYERS) - 1) * (0.95 + 0.4) + 0.95
    ax.set_ylim(0, top_of_stack + 1.3)
    ax.axis("off")

    box_height = 0.95
    gap = 0.4
    width = 8.6
    left = 0.7

    for index, (title, module, job, colour) in enumerate(reversed(LAYERS)):
        bottom = 0.9 + index * (box_height + gap)
        ax.add_patch(
            FancyBboxPatch(
                (left, bottom),
                width,
                box_height,
                boxstyle="round,pad=0.02,rounding_size=0.08",
                facecolor=colour,
                edgecolor="#333",
                linewidth=1.1,
            )
        )
        ax.text(left + 0.25, bottom + box_height * 0.62, title,
                fontsize=12, fontweight="bold", va="center")
        ax.text(left + width - 0.25, bottom + box_height * 0.62, module,
                fontsize=9, family="monospace", color="#444", va="center", ha="right")
        ax.text(left + 0.25, bottom + box_height * 0.24, job,
                fontsize=9.5, color="#222", va="center")

        if index < len(LAYERS) - 1:
            arrow_bottom = bottom + box_height
            ax.add_patch(
                FancyArrowPatch(
                    (left + width / 2, arrow_bottom + gap),
                    (left + width / 2, arrow_bottom + 0.06),
                    arrowstyle="-|>",
                    mutation_scale=16,
                    linewidth=1.4,
                    color="#333",
                )
            )

    ax.text(
        5.0,
        top_of_stack + 0.95,
        "Antar - a causal revenue-recovery controller",
        fontsize=15,
        fontweight="bold",
        ha="center",
    )
    ax.text(5.0, top_of_stack + 0.55,
            "It decides whether to act, not just how.",
            fontsize=10, ha="center", color="#555")
    ax.text(0.7, 0.28, CAPTION, fontsize=8.5, color="#444", va="top")

    out = Path(antar.__file__).parent.parent / "docs" / "img" / "architecture.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return out


def main() -> int:
    path = draw()
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
