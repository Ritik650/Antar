"""Every figure, from the artifacts. No figure is drawn from anything else.

    python tasks.py figures

Same discipline as `RESULTS.md`: a chart is a claim, and a chart drawn from numbers a
person typed is a claim with no provenance. Each function here reads one JSON artifact
and refuses — visibly, on the figure itself — if the artifact is missing.

Written with matplotlib defaults on purpose. A styled chart takes an hour and persuades
nobody who is checking the numbers.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # No display on CI, and none wanted.
import matplotlib.pyplot as plt

from antar.config import artifacts_dir

FIGURES = "figures"


def figures_dir() -> Path:
    path = artifacts_dir() / FIGURES
    path.mkdir(parents=True, exist_ok=True)
    return path


def load(name: str) -> dict | list | None:
    path = artifacts_dir() / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _missing(ax: Any, artifact: str) -> None:
    """Draw the absence rather than skipping it.

    A figure that quietly does not appear is indistinguishable from one nobody looked
    at. A figure that says NOT PRODUCED is a question someone will ask.
    """
    ax.text(
        0.5,
        0.5,
        f"NOT PRODUCED\n\n{artifact} is missing.\nRun `python tasks.py evaluate`.",
        ha="center",
        va="center",
        fontsize=11,
        color="crimson",
    )
    ax.set_xticks([])
    ax.set_yticks([])


def _save(fig: Any, name: str) -> Path:
    path = figures_dir() / name
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {path}")
    return path


# ---------------------------------------------------------------------------


def figure_three_policies() -> Path:
    """The comparison that matters, decomposed so the opt-out term is visible.

    Stacking recovery against harm is the whole argument in one picture: two policies
    recover real money and lose more of it to induced cancellations.
    """
    fig, ax = plt.subplots(figsize=(8, 4.5))
    data = load("allocation_base.json")
    if data is None:
        _missing(ax, "allocation_base.json")
        return _save(fig, "01_three_policies.png")

    rows = data["policies"]
    names = [r["policy"] for r in rows]
    incremental = [r["expected_incremental_rupees"] for r in rows]
    optout = [-r["expected_optout_loss_rupees"] for r in rows]
    cost = [-r["channel_cost_rupees"] for r in rows]
    net = [r["net_rupees"] for r in rows]

    ax.bar(names, incremental, label="expected incremental recovery", color="#2a7f62")
    ax.bar(names, optout, label="expected opt-out loss", color="#b3423f")
    ax.bar(names, cost, bottom=optout, label="channel cost", color="#8a8a8a")
    ax.plot(names, net, "o-", color="black", label="net")
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_ylabel("rupees (simulated)")
    ax.set_title("Three policies under a shared contact capacity")
    ax.legend(fontsize=8)
    fig.text(
        0.01, -0.04,
        "Simulated. See docs/SIMULATOR_CARD.md. Not a measured recovery rate (N6).",
        fontsize=7, color="#555",
    )
    return _save(fig, "01_three_policies.png")


def figure_phase_diagram() -> Path:
    """Where the result holds. The boundary is the contribution, not the point."""
    data = load("phase_diagram.json")
    if data is None:
        fig, ax = plt.subplots(figsize=(7, 4))
        _missing(ax, "phase_diagram.json")
        return _save(fig, "02_phase_diagram.png")

    cells = data["cells"]
    panels = sorted({c["panel"] for c in cells})
    fig, axes = plt.subplots(1, len(panels), figsize=(6 * len(panels), 4.5), squeeze=False)

    for ax, panel in zip(axes[0], panels, strict=True):
        panel_cells = [c for c in cells if c["panel"] == panel]
        xs = sorted({c["mean_self_heal"] for c in panel_cells})
        ys = sorted({c["mean_optout_sensitivity"] for c in panel_cells})
        grid = [[float("nan")] * len(xs) for _ in ys]
        for cell in panel_cells:
            row = ys.index(cell["mean_optout_sensitivity"])
            column = xs.index(cell["mean_self_heal"])
            grid[row][column] = cell["delta_per_1000_rupees"]

        image = ax.imshow(
            grid, origin="lower", aspect="auto", cmap="RdYlGn",
            extent=(min(xs), max(xs), min(ys), max(ys)),
        )
        ax.set_xlabel("mean self-heal probability")
        ax.set_ylabel("mean opt-out sensitivity")
        ax.set_title(f"Antar minus propensity, Rs/1,000 cycles\n{panel}", fontsize=10)
        fig.colorbar(image, ax=ax)

    fig.text(
        0.01, -0.03,
        "Green: Antar ahead. The pre-registered grid stops at opt-out 0.05; cells below "
        "that are a disclosed post-hoc extension (ADR-0020).",
        fontsize=7, color="#555",
    )
    return _save(fig, "02_phase_diagram.png")


def figure_specification_curve() -> Path:
    """540 defensible analyses, sorted. The headline is a distribution."""
    fig, ax = plt.subplots(figsize=(8, 4.5))
    data = load("specification_curve_base.json")
    if data is None:
        _missing(ax, "specification_curve_base.json")
        return _save(fig, "03_specification_curve.png")

    values = sorted(float(row["share"]) for row in data["specifications"])
    summary = data["summary"]

    ax.plot(range(len(values)), values, linewidth=1.2, color="#2f4b7c")
    ax.axhline(summary["threshold"], color="crimson", linestyle="--", linewidth=1,
               label=f"pre-registered bar ({summary['threshold']})")
    ax.axhline(summary["median"], color="#555", linestyle=":", linewidth=1,
               label=f"median ({summary['median']})")
    if summary.get("pre_registered_share") is not None:
        ax.axhline(summary["pre_registered_share"], color="#e08b00", linewidth=1,
                   label=f"our pre-registered spec (pct {summary.get('pre_registered_percentile')})")
    ax.set_xlabel("specification, sorted")
    ax.set_ylabel("negative-uplift share")
    ax.set_title("Specification curve: the same question asked every defensible way")
    ax.legend(fontsize=8)
    return _save(fig, "03_specification_curve.png")


def figure_detection() -> Path:
    """Precision and recall per class, with the rupee cost of getting it wrong.

    The cost bars matter more than the F1 bars: a class Antar misreads cheaply is not
    the same problem as one it misreads expensively, and a macro average hides that.
    """
    fig, (ax_pr, ax_cost) = plt.subplots(1, 2, figsize=(12, 4.5))
    payload = load("detection_base.json")
    if payload is None:
        _missing(ax_pr, "detection_base.json")
        _missing(ax_cost, "detection_base.json")
        return _save(fig, "04_detection.png")

    per_class = payload["detection"]["per_class"]
    names = sorted(per_class)
    width = 0.38
    positions = range(len(names))

    ax_pr.bar([p - width / 2 for p in positions],
              [per_class[n]["precision"] for n in names], width, label="precision")
    ax_pr.bar([p + width / 2 for p in positions],
              [per_class[n]["recall"] for n in names], width, label="recall")
    ax_pr.set_xticks(list(positions))
    ax_pr.set_xticklabels(names, rotation=30, ha="right", fontsize=8)
    ax_pr.set_ylim(0, 1)
    ax_pr.set_title("L2 detection, control arm only")
    ax_pr.legend(fontsize=8)

    ax_cost.bar(list(positions),
                [per_class[n]["wrong_action_cost_rupees"] for n in names], color="#b3423f")
    ax_cost.set_xticks(list(positions))
    ax_cost.set_xticklabels(names, rotation=30, ha="right", fontsize=8)
    ax_cost.set_ylabel("rupees")
    ax_cost.set_title("Cost of the wrong action, by true class")
    return _save(fig, "04_detection.png")


def figure_batch_funnel() -> Path:
    """Where 100% of at-risk events go. Abstention is the product, not a gap."""
    fig, ax = plt.subplots(figsize=(8, 4))
    data = load("batch_base.json")
    if data is None:
        _missing(ax, "batch_base.json")
        return _save(fig, "05_batch_funnel.png")

    labels = ["contacted", "abstained", "refused by gate", "of which holdout"]
    values = [data["contacted"], data["abstained"], data["refused"], data["control"]]
    colours = ["#2a7f62", "#8a8a8a", "#b3423f", "#2f4b7c"]

    ax.barh(labels, values, color=colours)
    for index, value in enumerate(values):
        ax.text(value, index, f" {value}", va="center", fontsize=9)
    ax.set_xlabel(f"events (of {data['events']})")
    ax.set_title("What happened to every at-risk event")
    fig.text(
        0.01, -0.06,
        "The holdout overlaps 'abstained': a control customer is decided on and then "
        "deliberately not acted on (N3).",
        fontsize=7, color="#555",
    )
    return _save(fig, "05_batch_funnel.png")


def main() -> int:
    from scripts.make_architecture_diagram import draw

    print("figures (from artifacts only):")
    print(f"  wrote {draw()}")
    written = [
        figure_three_policies(),
        figure_phase_diagram(),
        figure_specification_curve(),
        figure_detection(),
        figure_batch_funnel(),
    ]
    print(f"\n  {len(written)} figures in {figures_dir()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
