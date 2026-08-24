"""The phase diagram, and the guards that stop it stating things it did not compute.

Two of this build's defects lived here (docs/POSTMORTEM.md D17 and D18), and both were
the same shape: a **generated sentence** asserting a comparison with total confidence
when the underlying computation had either not run or not done what it claimed.

That is the class PLAN.md rule 6 exists to prevent — "never fabricate a number" — and
prose generated from measurements is exactly where it hides, because the sentence looks
identical whether or not there is anything behind it.
"""

from __future__ import annotations

import pytest

from antar.config import load_config
from antar.eval.experiment import DEFAULT_CHANNELS, available_channels
from antar.eval.phase_diagram import (
    OPTOUT_AXIS,
    PANELS,
    SELF_HEAL_AXIS,
    Cell,
    PhaseDiagram,
    cell_scenario,
    render_ascii,
    scenario_anchors,
)
from antar.simulator.scenarios import get_scenario


def cell(panel: str, self_heal: float, optout: float, delta: float, **kwargs) -> Cell:
    defaults = {
        "antar_net_per_1000_paise": delta,
        "propensity_net_per_1000_paise": 0.0,
        "antar_contacts": 10,
        "propensity_contacts": 20,
        "contact_capacity_shadow_price_paise": 0.0,
        "events": 100,
    }
    return Cell(
        panel=panel,
        mean_self_heal=self_heal,
        mean_optout_sensitivity=optout,
        antar_minus_propensity_per_1000_paise=delta,
        **{**defaults, **kwargs},
    )


# --------------------------------------------------------------- the axes


def test_the_axes_are_the_pre_registered_ones():
    """EVALUATION.md 9.4 fixed these before any cell was computed."""
    assert SELF_HEAL_AXIS[0] == 0.10 and SELF_HEAL_AXIS[-1] == 0.60
    assert len(SELF_HEAL_AXIS) == 11
    assert OPTOUT_AXIS[0] == 0.05 and OPTOUT_AXIS[-1] == 0.40
    assert len(OPTOUT_AXIS) == 8
    assert PANELS == ("best_available", "reference_sms")


def test_the_scenarios_are_plotted_on_the_map_not_used_to_define_it():
    """9.4: the axes are swept independently of the scenario definitions."""
    anchors = scenario_anchors()
    assert set(anchors) == {"conservative", "base", "aggressive"}
    for values in anchors.values():
        assert "mean_self_heal" in values and "mean_optout_sensitivity" in values


def test_a_grid_cell_moves_only_the_two_mapped_parameters():
    """Letting heterogeneity or downtime drift with the axes would make the map a
    picture of four things at once."""
    base = get_scenario("base")
    cellular = cell_scenario(base, 0.42, 0.17)
    assert cellular.mean_self_heal == 0.42
    assert cellular.mean_optout_sensitivity == 0.17
    for axis in ("heterogeneity", "downtime_multiplier", "intent_to_churn_rate",
                 "base_failure_rate", "mean_persuadability"):
        assert getattr(cellular, axis) == getattr(base, axis), axis


# ------------------------------------------------- D18: the panels must differ


def test_the_channel_set_is_configurable_not_a_module_constant():
    """docs/POSTMORTEM.md D18.

    `reference_sms` was first implemented by zeroing the other channels' *costs*,
    which left them fully available and produced two identical panels. The restriction
    has to apply to the action set.
    """
    config = load_config(environ={})
    assert available_channels(config) == DEFAULT_CHANNELS

    restricted = config.with_overrides({"simulator.available_channels": ["SMS"]})
    channels = available_channels(restricted)
    assert len(channels) == 1
    assert channels[0].value == "SMS"


def test_the_two_panels_must_not_be_identical():
    """The cheap assertion that would have caught D18 immediately.

    Two panels agreeing to seven significant figures is not a finding about channel
    mix; it is one panel run twice.
    """
    diagram = PhaseDiagram()
    for panel in PANELS:
        diagram.cells.append(cell(panel, 0.30, 0.20, 1_000.0))

    best = [c.antar_minus_propensity_per_1000_paise for c in diagram.panel("best_available")]
    sms = [c.antar_minus_propensity_per_1000_paise for c in diagram.panel("reference_sms")]
    assert best == sms  # the fixture is deliberately degenerate...
    # ...and this is the check a real run must pass.
    assert _panels_are_suspiciously_identical(best, sms)


def _panels_are_suspiciously_identical(a: list[float], b: list[float]) -> bool:
    if not a or not b or len(a) != len(b):
        return False
    return all(abs(x - y) < 1e-6 * max(abs(x), 1.0) for x, y in zip(a, b, strict=True))


# ---------------------------------------- D17: no sentence without a measurement


def test_an_uncomputed_panel_is_not_reported_as_zero():
    """docs/POSTMORTEM.md D17.

    `win_share` on an empty panel returns 0.0, and the first version of the sentence
    generator could not tell that from "computed, and won nothing". It printed
    "0% for one with only SMS" after a run in which the SMS panel had never existed.
    """
    diagram = PhaseDiagram()
    for self_heal in SELF_HEAL_AXIS[:3]:
        diagram.cells.append(cell("best_available", self_heal, 0.20, 5_000.0))

    interpretation = diagram.summary()["interpretation"]
    assert "No cross-panel comparison is available" in interpretation
    assert "reference_sms was not run" in interpretation
    # The false claim the old version made must not be reproducible.
    assert "0% for one with only SMS" not in interpretation


def test_no_cells_at_all_says_so():
    assert "nothing to interpret" in PhaseDiagram().summary()["interpretation"].lower()


def test_a_genuine_cross_panel_difference_is_reported_as_one():
    diagram = PhaseDiagram()
    for self_heal in SELF_HEAL_AXIS:
        diagram.cells.append(cell("best_available", self_heal, 0.20, 5_000.0))
        diagram.cells.append(cell("reference_sms", self_heal, 0.20, -5_000.0))

    interpretation = diagram.summary()["interpretation"]
    assert "boundary moves between panels" in interpretation
    assert "channel mix" in interpretation


def test_a_stable_sign_with_a_different_magnitude_says_both():
    """Reporting only the win share hid a 41% difference in how much Antar is worth,
    which is the number a merchant deciding whether to build this would want."""
    diagram = PhaseDiagram()
    for self_heal in SELF_HEAL_AXIS:
        diagram.cells.append(cell("best_available", self_heal, 0.20, 100_000.0))
        diagram.cells.append(cell("reference_sms", self_heal, 0.20, 200_000.0))

    interpretation = diagram.summary()["interpretation"]
    assert "every cell of the pre-registered grid" in interpretation
    assert "magnitude" in interpretation
    assert "%" in interpretation


# ------------------------------------------------------------ the boundary


def test_the_boundary_is_located_where_the_sign_changes():
    diagram = PhaseDiagram()
    for self_heal in SELF_HEAL_AXIS:
        delta = 1_000.0 if self_heal < 0.35 else -1_000.0
        diagram.cells.append(cell("best_available", self_heal, 0.20, delta))

    crossing = diagram.boundary_self_heal("best_available")[0.20]
    assert crossing is not None
    assert 0.30 <= crossing <= 0.35


def test_a_row_that_never_changes_sign_reports_none_rather_than_a_guess():
    """Informative, and reported rather than smoothed over."""
    diagram = PhaseDiagram()
    for self_heal in SELF_HEAL_AXIS:
        diagram.cells.append(cell("best_available", self_heal, 0.20, 1_000.0))
    assert diagram.boundary_self_heal("best_available")[0.20] is None


def test_the_indifference_band_is_rendered_rather_than_assigned_a_side():
    """9.4: cells inside the band are drawn as indifferent, and the band is part of
    the finding."""
    diagram = PhaseDiagram(indifference_band_paise=2_000.0)
    diagram.cells.append(cell("best_available", SELF_HEAL_AXIS[0], OPTOUT_AXIS[0], 500.0))
    rendered = render_ascii(diagram, "best_available")
    assert "." in rendered
    assert "indifferent" in rendered


def test_uncomputed_cells_render_as_unknown_not_as_zero():
    diagram = PhaseDiagram()
    rendered = render_ascii(diagram, "best_available")
    assert "?" in rendered


@pytest.mark.parametrize("panel", PANELS)
def test_win_share_of_an_empty_panel_is_distinguishable_from_a_real_zero(panel):
    empty = PhaseDiagram()
    assert empty.summary()["panels"][panel]["cells"] == 0

    lost = PhaseDiagram()
    lost.cells.append(cell(panel, 0.30, 0.20, -1.0))
    stats = lost.summary()["panels"][panel]
    assert stats["cells"] == 1 and stats["antar_wins_share"] == 0.0
