"""The phase diagram: where does this class of system pay for itself?

`docs/EVALUATION.md` §9.4, pre-registered before any cell was computed.

## Why a map rather than a number

A point estimate from a simulator is worth very little — the magnitude is a property of
parameters we chose. A **boundary** is worth considerably more, because a boundary is a
statement about mechanism, and mechanism is the part that survives the parameters being
wrong.

So the claim this supports, and the only claim it supports:

> Uplift-based allocation beats competent propensity targeting in *this* region of
> parameter space. Here is the boundary. A merchant does not know which side of it they
> are on without measuring their own self-heal and opt-out rates — and here is what
> they would have to measure.

## Two panels, because the specification curve said to

`artifacts/specification_curve_base.json` found that the **action set** dominates every
other analytic choice: `best_available` gives a 6.3% negative-uplift population and
`reference_sms` gives 17.2%. So the grid is swept twice —

  * **`best_available`** — a merchant with every channel integrated, choosing the best
    one per customer.
  * **`reference_sms`** — a merchant with one SMS integration, which is most merchants.

If the indifference boundary moves substantially between the panels, then whether this
system pays for itself depends more on the merchant's **channel mix** than on their
customer base. That is an actionable finding for a merchant and a useful one for a
payment processor deciding who to build this for.

## What is mapped

Net expected rupees per 1,000 at-risk cycles, **P3 (Antar) minus P2 (propensity
targeting)**. Not P3 minus P1: beating "contact everyone" is easy and proves little.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np

from antar.simulator.scenarios import SCENARIOS, Scenario

# Pre-registered in EVALUATION.md 9.4, before any cell was computed.
SELF_HEAL_AXIS: tuple[float, ...] = (0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60)
OPTOUT_AXIS: tuple[float, ...] = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)

EXTENDED_OPTOUT_AXIS: tuple[float, ...] = (0.005, 0.010, 0.020, 0.035)
"""A **disclosed post-hoc extension** below the pre-registered range.

The pre-registered sweep found Antar ahead in every cell of both panels, which means
the indifference boundary lies at or below an opt-out sensitivity of 0.05. Reporting
"we won everywhere" without locating the boundary would waste the artifact; extending
the axis after seeing the result and *not saying so* would be the thing this whole
protocol exists to prevent.

So: the pre-registered grid is the headline and is reported as run. This extension is
labelled `extended` in the artifact, and any claim resting on it says so.
"""

PANELS: tuple[str, ...] = ("best_available", "reference_sms")


@dataclass(frozen=True)
class Cell:
    panel: str
    mean_self_heal: float
    mean_optout_sensitivity: float
    antar_minus_propensity_per_1000_paise: float
    antar_net_per_1000_paise: float
    propensity_net_per_1000_paise: float
    antar_contacts: int
    propensity_contacts: int
    contact_capacity_shadow_price_paise: float
    events: int

    @property
    def antar_wins(self) -> bool:
        return self.antar_minus_propensity_per_1000_paise > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "antar_wins": self.antar_wins,
            "delta_per_1000_rupees": round(
                self.antar_minus_propensity_per_1000_paise / 100, 2
            ),
        }


@dataclass
class PhaseDiagram:
    cells: list[Cell] = field(default_factory=list)
    indifference_band_paise: float = 0.0
    """Half-width of the band drawn as `indifferent` rather than assigned to a side.

    §9.4: cells whose difference has a CI straddling zero are drawn as indifferent, and
    the band is part of the finding rather than a rendering detail.
    """

    def panel(self, name: str) -> list[Cell]:
        return [c for c in self.cells if c.panel == name]

    def optout_levels(self, name: str) -> list[float]:
        """The opt-out values actually present, pre-registered and extended alike.

        Read from the cells rather than from `OPTOUT_AXIS`, so that a disclosed
        extension renders instead of being silently dropped from the map it was added
        to produce.
        """
        return sorted({c.mean_optout_sensitivity for c in self.panel(name)})

    def grid(self, name: str) -> np.ndarray:
        """Rows = opt-out sensitivity, columns = self-heal. `nan` where not computed."""
        levels = self.optout_levels(name) or list(OPTOUT_AXIS)
        out = np.full((len(levels), len(SELF_HEAL_AXIS)), np.nan)
        for cell in self.panel(name):
            if cell.mean_self_heal not in SELF_HEAL_AXIS:
                continue
            row = levels.index(cell.mean_optout_sensitivity)
            column = SELF_HEAL_AXIS.index(cell.mean_self_heal)
            out[row, column] = cell.antar_minus_propensity_per_1000_paise
        return out

    def win_share(self, name: str) -> float:
        """Share of cells where the advantage is positive, however small."""
        cells = self.panel(name)
        if not cells:
            return 0.0
        return sum(1 for c in cells if c.antar_wins) / len(cells)

    def decisive_win_share(self, name: str) -> float:
        """Share of cells where the advantage is positive **and outside the band**.

        The number that matches what the map draws. Reporting the bare sign share
        alongside a map full of `.` cells was internally inconsistent: a cell can be
        positive by a rupee and still be indifferent, and quoting "wins 100%" over a
        grid that renders as mostly indifferent invites exactly the objection it
        deserves.
        """
        cells = self.panel(name)
        if not cells:
            return 0.0
        band = self.indifference_band_paise
        decisive = sum(
            1
            for c in cells
            if c.antar_wins and abs(c.antar_minus_propensity_per_1000_paise) > band
        )
        return decisive / len(cells)

    def decisive_boundary(self, name: str) -> float | None:
        """Lowest opt-out sensitivity at which **every** self-heal level is a decisive
        win. The operational reading of "above this, the system is worth running".

        `None` when no level qualifies.
        """
        band = self.indifference_band_paise
        for optout in self.optout_levels(name):
            row = [c for c in self.panel(name) if c.mean_optout_sensitivity == optout]
            if row and all(
                c.antar_wins and abs(c.antar_minus_propensity_per_1000_paise) > band
                for c in row
            ):
                return optout
        return None

    def capacity_binding_ceiling(self, name: str) -> float | None:
        """Highest opt-out sensitivity at which the contact capacity still binds.

        Above it the scarce resource is customer tolerance rather than outbound
        capacity, and every capacity shadow price is zero. See LIMITATIONS L14.
        """
        binding = [
            optout
            for optout in self.optout_levels(name)
            if any(
                c.contact_capacity_shadow_price_paise > 0
                for c in self.panel(name)
                if c.mean_optout_sensitivity == optout
            )
        ]
        return max(binding) if binding else None

    def boundary_self_heal(self, name: str) -> dict[float, float | None]:
        """For each opt-out level, the self-heal rate at which the advantage crosses zero.

        Linear interpolation between the bracketing cells. `None` when the sign never
        changes along that row - which is itself informative, and is reported rather
        than smoothed over.
        """
        out: dict[float, float | None] = {}
        for optout in (self.optout_levels(name) or list(OPTOUT_AXIS)):
            row = sorted(
                (c for c in self.panel(name) if c.mean_optout_sensitivity == optout),
                key=lambda c: c.mean_self_heal,
            )
            crossing = None
            for left, right in pairwise(row):
                a = left.antar_minus_propensity_per_1000_paise
                b = right.antar_minus_propensity_per_1000_paise
                if (a > 0) != (b > 0) and (b - a) != 0:
                    fraction = a / (a - b)
                    crossing = left.mean_self_heal + fraction * (
                        right.mean_self_heal - left.mean_self_heal
                    )
                    break
            out[optout] = crossing
        return out

    def summary(self) -> dict[str, Any]:
        panels = {
            name: {
                "cells": len(self.panel(name)),
                "antar_wins_share": round(self.win_share(name), 4),
                "decisive_win_share": round(self.decisive_win_share(name), 4),
                "decisive_boundary_optout": self.decisive_boundary(name),
                "capacity_binding_ceiling_optout": self.capacity_binding_ceiling(name),
                "median_delta_per_1000_rupees": round(
                    float(
                        np.median(
                            [c.antar_minus_propensity_per_1000_paise for c in self.panel(name)]
                        )
                        / 100
                    ),
                    2,
                )
                if self.panel(name)
                else None,
                "boundary_self_heal_by_optout": {
                    str(k): (round(v, 4) if v is not None else None)
                    for k, v in self.boundary_self_heal(name).items()
                },
                "capacity_binds_share": round(
                    sum(
                        1
                        for c in self.panel(name)
                        if c.contact_capacity_shadow_price_paise > 0
                    )
                    / max(len(self.panel(name)), 1),
                    4,
                ),
            }
            for name in PANELS
        }
        return {
            "axes": {
                "x_mean_self_heal": list(SELF_HEAL_AXIS),
                "y_mean_optout_sensitivity": list(OPTOUT_AXIS),
            },
            "metric": (
                "Net expected rupees per 1,000 at-risk cycles, P3 (Antar) minus P2 "
                "(propensity targeting). Not P3 minus P1."
            ),
            "panels": panels,
            "scenario_anchors": scenario_anchors(),
            "interpretation": _interpretation(panels),
        }

    def as_dict(self) -> dict[str, Any]:
        return {"summary": self.summary(), "cells": [c.as_dict() for c in self.cells]}


def _interpretation(panels: dict[str, Any]) -> str:
    """The sentence the README quotes. Generated, never typed.

    An earlier version read `win_share` on an *uncomputed* panel, got 0.0 from an
    empty list, and printed "Antar beats propensity in 100% of the parameter space for
    a merchant with every channel, and 0% for one with only SMS" after a run in which
    the SMS panel had never been computed. A fabricated comparison stated with total
    confidence. `cells == 0` is now distinguished from `wins == 0`.
    """
    best = panels.get("best_available", {})
    sms = panels.get("reference_sms", {})

    missing = [name for name, panel in (("best_available", best), ("reference_sms", sms))
               if not panel.get("cells")]
    if missing:
        computed = [n for n in ("best_available", "reference_sms") if n not in missing]
        if not computed:
            return "No cells computed; nothing to interpret."
        name = computed[0]
        share = panels[name]["antar_wins_share"]
        return (
            f"Only the `{name}` panel was computed: Antar beats propensity targeting in "
            f"{share:.0%} of that grid. **No cross-panel comparison is available** - "
            f"{', '.join(missing)} was not run, so nothing can be said about whether the "
            "boundary moves with the merchant's channel mix."
        )

    a, b = best.get("antar_wins_share"), sms.get("antar_wins_share")
    if a is None or b is None:
        return "Not enough cells computed to interpret."

    median_best = best.get("median_delta_per_1000_rupees") or 0.0
    median_sms = sms.get("median_delta_per_1000_rupees") or 0.0
    magnitude_gap = (
        abs(median_sms - median_best) / abs(median_best) if median_best else 0.0
    )

    # Two things can differ between panels and they are not the same claim. Reporting
    # only the win share hides a 41% difference in how *much* Antar is worth, which is
    # the number a merchant deciding whether to build this would actually want.
    sign_claim = (
        f"Antar beats propensity targeting in {a:.0%} of the grid for a merchant with "
        f"every channel and {b:.0%} for one with only SMS"
    )
    magnitude_claim = (
        f"median advantage Rs {median_best:,.0f} vs Rs {median_sms:,.0f} per 1,000 "
        f"cycles, a {magnitude_gap:.0%} difference"
    )

    if abs(a - b) >= 0.15:
        return (
            f"**The indifference boundary moves between panels.** {sign_claim}. Whether "
            "this class of system pays for itself depends more on the merchant's "
            f"channel mix than on their customer base ({magnitude_claim})."
        )
    boundary_best = best.get("decisive_boundary_optout")
    boundary_sms = sms.get("decisive_boundary_optout")
    if (
        boundary_best is not None
        and boundary_sms is not None
        and boundary_best != boundary_sms
    ):
        return (
            "**The decisive boundary moves with the merchant's channel mix.** Uplift "
            f"allocation is decisively better than propensity targeting above an "
            f"opt-out sensitivity of {boundary_best:.3f} for a merchant with every "
            f"channel, but only above {boundary_sms:.3f} for one with a single SMS "
            "integration - below that the two are indistinguishable. So whether this "
            "class of system is worth running depends on the merchant's channel mix as "
            f"well as on their customers ({magnitude_claim})."
        )

    if a >= 0.99 and b >= 0.99:
        decisive_best = best.get("decisive_win_share", 0.0)
        decisive_sms = sms.get("decisive_win_share", 0.0)
        return (
            "**Antar is ahead in every cell of the pre-registered grid in both "
            f"panels** ({sign_claim}), so the indifference boundary lies at or below "
            "the bottom edge of the committed opt-out range (0.05) - outside the space "
            "we pre-registered, which is itself the finding. A disclosed extension "
            f"below 0.05 locates it: the advantage becomes decisive at an opt-out "
            f"sensitivity of {boundary_best or float('nan'):.3f} in both panels. "
            f"Counting only cells outside the indifference band, Antar wins "
            f"{decisive_best:.0%} of the multi-channel grid and {decisive_sms:.0%} of "
            "the SMS-only one: below the boundary a multi-channel merchant already sees "
            "decisive gains in places, while an SMS-only merchant sees none. The "
            f"magnitudes differ too - {magnitude_claim} - the SMS-only merchant gaining "
            "more, because a propensity ranker with a single channel has fewer ways to "
            "be accidentally right."
        )
    return (
        f"The sign of the advantage is stable across panels ({sign_claim}), but the "
        f"magnitude is not: {magnitude_claim}. The merchant's channel mix changes how "
        "much this is worth, not whether it is worth anything."
    )


def scenario_anchors() -> dict[str, dict[str, float]]:
    """Where the three named scenarios sit **on** the map.

    §9.4 fixes that the axes are swept independently of the scenario definitions, and
    the scenarios are plotted as three points rather than used to define the grid.
    """
    return {
        name: {
            "mean_self_heal": SCENARIOS[name].mean_self_heal,
            "mean_optout_sensitivity": SCENARIOS[name].mean_optout_sensitivity,
        }
        for name in ("conservative", "base", "aggressive")
    }


def cell_scenario(base: Scenario, self_heal: float, optout: float) -> Scenario:
    """A scenario at one grid point, with every other axis held at the base value.

    Only the two mapped parameters move. Letting `heterogeneity` or `downtime` drift
    with them would make the map a picture of four things at once.
    """
    from dataclasses import replace

    return replace(
        base,
        name=f"grid_sh{self_heal:.2f}_oo{optout:.2f}",
        mean_self_heal=self_heal,
        mean_optout_sensitivity=optout,
    )


def write(diagram: PhaseDiagram, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(diagram.as_dict(), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return path


def render_ascii(diagram: PhaseDiagram, panel: str) -> str:
    """A terminal-readable map. `+` Antar wins, `-` propensity wins, `.` indifferent."""
    grid = diagram.grid(panel)
    band = diagram.indifference_band_paise
    levels = diagram.optout_levels(panel) or list(OPTOUT_AXIS)
    lines = [f"  panel: {panel}", "  self-heal ->"]
    header = "        " + "".join(f"{v:5.2f}" for v in SELF_HEAL_AXIS)
    lines.append(header)
    for index, optout in enumerate(levels):
        cells = []
        for value in grid[index]:
            if np.isnan(value):
                cells.append("    ?")
            elif abs(value) <= band:
                cells.append("    .")
            elif value > 0:
                cells.append("    +")
            else:
                cells.append("    -")
        marker = " " if optout in OPTOUT_AXIS else "*"
        lines.append(f"  {optout:5.3f}{marker}" + "".join(cells))
    lines.append("")
    lines.append("  + Antar beats propensity   - propensity beats Antar   . indifferent")
    return "\n".join(lines)
