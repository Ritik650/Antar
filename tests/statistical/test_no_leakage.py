"""No latent variable may reach any Antar layer.

docs/SIMULATOR_CARD.md section 10 singles this one out:

> The latents are the answer key. If any of them leaks into
> `antar/decide/features.py` - even indirectly, through a derived column - the uplift
> models will look spectacular and mean nothing.

Three independent checks, because one is not enough:

  1. **Import-graph.** No module in the five production layers may import from
     `antar.simulator`. This catches the direct route.
  2. **Field-name.** No latent field name may appear as a feature column. This
     catches the copy-paste route.
  3. **Runtime.** The objects handed to the layers carry no latent attribute at all,
     so there is nothing to read even if someone tried. This catches the route the
     other two miss.
"""

from __future__ import annotations

import ast
import importlib
import json
import pkgutil
from pathlib import Path

import pytest

from antar.simulator.latents import CustomerLatents, LatentStore
from antar.simulator.scenarios import BASE

pytestmark = pytest.mark.statistical

REPO = Path(__file__).resolve().parents[2]
PACKAGE = REPO / "antar"

# The layers that must never see the answer key. `antar.eval` is deliberately absent:
# the evaluation harness is allowed to know the truth, because comparing an estimate
# against ground truth is its entire job.
PRODUCTION_LAYERS = ("signals", "detect", "decide", "policy", "act", "audit")

# Fields that live in the latent vector but that a merchant genuinely observes, and
# may therefore legitimately reach a feature builder.
#
# This allowlist is how a leakage test gets quietly defanged, so: it has exactly one
# member, each member carries a justification, and `test_the_observable_allowlist_
# stays_small` fails if it grows. Anything added here must be a quantity a real
# merchant has in their own database, not merely one that seems harmless.
OBSERVABLE_BY_DESIGN: dict[str, str] = {
    "tenure_months": (
        "How long the customer has been subscribed. Sits in the latent vector because "
        "the response model uses it, but every merchant knows it from their own "
        "subscription records - withholding it would model a merchant who cannot read "
        "their own database."
    ),
}

LATENT_FIELDS = (
    frozenset(CustomerLatents.__dataclass_fields__) - {"customer_id"} - set(OBSERVABLE_BY_DESIGN)
)


def _imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_no_production_layer_imports_the_simulator():
    offenders: list[str] = []
    for layer in PRODUCTION_LAYERS:
        for path in (PACKAGE / layer).rglob("*.py"):
            for module in _imports_of(path):
                if module.startswith("antar.simulator"):
                    offenders.append(f"{path.relative_to(REPO)} imports {module}")
    assert not offenders, "the answer key is reachable from a production layer:\n" + "\n".join(
        offenders
    )


def test_no_production_layer_imports_the_evaluation_harness():
    """`antar.eval` may read ground truth, so importing it upward is the same leak
    by a longer route."""
    offenders: list[str] = []
    for layer in PRODUCTION_LAYERS:
        for path in (PACKAGE / layer).rglob("*.py"):
            for module in _imports_of(path):
                if module.startswith("antar.eval"):
                    offenders.append(f"{path.relative_to(REPO)} imports {module}")
    assert not offenders, "\n".join(offenders)


def _denylist_string_lines(tree: ast.AST) -> set[int]:
    """Line numbers of string constants inside a `FORBIDDEN_*` assignment.

    `antar/decide/features.py` deliberately names every latent in a denylist, so that
    `build_frame` can raise if one ever appears. Naming a field in order to *reject*
    it is the opposite of leaking it, and flagging that would train the reader to
    ignore this test - which is how a guard stops working.

    Scoped precisely to denylist assignments rather than exempting the file, because
    `features.py` is the single module this test most needs to cover.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        # Both forms. `FORBIDDEN_COLUMNS: frozenset[str] = frozenset({...})` is an
        # AnnAssign, not an Assign, and handling only the latter silently exempted
        # nothing while appearing to work.
        if isinstance(node, ast.Assign):
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = {node.target.id}
            value = node.value
        else:
            continue

        if value is None or not any(name.startswith("FORBIDDEN") for name in names):
            continue
        for child in ast.walk(value):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                lines.add(child.lineno)
    return lines


def test_no_latent_field_name_appears_in_a_production_module():
    """Catches a latent copied across by hand rather than imported."""
    offenders: list[str] = []
    for layer in PRODUCTION_LAYERS:
        for path in (PACKAGE / layer).rglob("*.py"):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
            allowed_lines = _denylist_string_lines(tree)
            for node in ast.walk(tree):
                # String literals are how a feature column would be named.
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value in LATENT_FIELDS and node.lineno not in allowed_lines:
                        offenders.append(
                            f"{path.relative_to(REPO)}:{node.lineno}: literal {node.value!r}"
                        )
                elif isinstance(node, ast.Name) and node.id in LATENT_FIELDS:
                    offenders.append(f"{path.relative_to(REPO)}:{node.lineno}: name {node.id}")
    assert not offenders, "latent field name used in a production layer:\n" + "\n".join(offenders)


def test_the_objects_handed_to_the_layers_carry_no_latent():
    """The runtime check. `CustomerContext` is what the policy layer sees."""
    from antar.signals.schemas import AtRiskEvent, CustomerContext

    for model in (CustomerContext, AtRiskEvent):
        exposed = set(model.model_fields) & LATENT_FIELDS
        assert not exposed, f"{model.__name__} exposes latent fields: {exposed}"


def test_the_generated_batch_keeps_ground_truth_out_of_the_event_stream():
    """Events are what the layers consume. None may carry its own answer."""
    from tests.statistical.helpers import batch as cached_batch

    batch = cached_batch()
    for event in batch.events[:200]:
        blob = event.model_dump_json()
        for field in LATENT_FIELDS:
            assert field not in blob, f"{event.event_id} leaks {field}"
        # The true failure class is held in a side table keyed by event id, not on
        # the event, precisely so that consuming the stream cannot reveal it.
        assert "true_failure_class" not in blob
        assert "failure_class" not in blob


def test_latents_are_only_reachable_through_the_store():
    """A guard on the shape of the seam rather than on any single caller."""
    store = LatentStore(BASE, 20260822)
    latents = store.get("cust_000001")
    assert isinstance(latents, CustomerLatents)
    assert set(latents.as_row()) - {"customer_id"} != set(), "as_row must expose the truth"
    # ...and `as_row` is used only by the simulator and the evaluation harness.
    callers: list[str] = []
    for layer in PRODUCTION_LAYERS:
        for path in (PACKAGE / layer).rglob("*.py"):
            if "as_row" in path.read_text(encoding="utf-8"):
                callers.append(str(path.relative_to(REPO)))
    assert not callers, f"as_row() called from a production layer: {callers}"


def test_the_denylist_exemption_is_narrow():
    """The exemption above must not become a way to launder a real leak.

    A latent name in an ordinary assignment - not a FORBIDDEN_* denylist - is still an
    offence, and this proves the scan still sees it.
    """
    leaky = ast.parse("SOMETHING = {'p_self_heal_base'}")
    assert _denylist_string_lines(leaky) == set()

    denylist = ast.parse("FORBIDDEN_COLUMNS = {'p_self_heal_base'}")
    assert _denylist_string_lines(denylist) == {1}

    # The annotated form is what features.py actually uses, and handling only the
    # plain form exempted nothing while appearing to work.
    annotated = ast.parse("FORBIDDEN_COLUMNS: frozenset[str] = frozenset({'salary_day'})")
    assert _denylist_string_lines(annotated) == {1}


def test_the_feature_denylist_actually_names_the_latents():
    """The exemption is only defensible if the denylist is doing its job."""
    from antar.decide.features import FORBIDDEN_COLUMNS

    assert FORBIDDEN_COLUMNS | {"channel_response"} >= LATENT_FIELDS, (
        "a latent is missing from antar/decide/features.py FORBIDDEN_COLUMNS: "
        f"{sorted(LATENT_FIELDS - FORBIDDEN_COLUMNS)}"
    )


def test_the_observable_allowlist_stays_small():
    """The allowlist is the obvious way to defeat this whole file.

    One entry today. If a future change needs a second, that change should be hard
    enough to require editing this number and writing the justification, rather than
    easy enough to do while chasing a red test.
    """
    assert len(OBSERVABLE_BY_DESIGN) <= 1, (
        f"observable allowlist grew to {sorted(OBSERVABLE_BY_DESIGN)}; every addition "
        "weakens the leakage guarantee and needs an ADR"
    )
    for field, justification in OBSERVABLE_BY_DESIGN.items():
        assert len(justification) > 60, f"{field} needs a real justification, not a label"


def test_observable_fields_agree_with_the_truth():
    """docs/POSTMORTEM.md D7.

    An observable is only safe to expose if it is the *same number* as the latent it
    mirrors. Two independent draws of "tenure" would not leak the answer - they would
    do something arguably worse, silently replacing a real signal with noise and
    making every uplift model look worse than it is for a reason nobody could find.
    """
    from tests.statistical.helpers import batch as cached_batch

    batch = cached_batch()
    assert batch.latents is not None
    checked = 0
    for customer_id, context in list(batch.customers.items())[:300]:
        truth = batch.latents.get(customer_id)
        for field in OBSERVABLE_BY_DESIGN:
            assert getattr(context, field) == getattr(truth, field), (
                f"{customer_id}: observable {field} disagrees with the latent it mirrors "
                f"({getattr(context, field)} vs {getattr(truth, field)})"
            )
            checked += 1
    assert checked > 0


# ---------------------------------------------------------------------------
# Canary checks: leakage that travels by DATA rather than by import.
#
# The static checks above would all have passed while `antar/detect/pipeline.py`
# read `batch.true_failure_class` to build the mandate FSM - the leak that reported a
# MANDATE_REVOKED recall of 1.000 (POSTMORTEM D10). It arrived as a function argument,
# so there was no import to find and no field name to match.
#
# Finding the specific instance is a fix. Building the general defence is the lesson.
# ---------------------------------------------------------------------------


def assert_no_canary(payload: object, *, context: str) -> None:
    """Fail if any answer-key marker is reachable from `payload`.

    The single assertion every feature builder and every model input must pass. Kept
    here rather than inside `antar/` on purpose: the production code must not import
    the thing that knows what the answer key looks like.
    """
    from antar.simulator.latents import CANARY_PREFIX

    rendered = json.dumps(payload, default=str)
    assert CANARY_PREFIX not in rendered, (
        f"answer-key canary reachable from {context}. Something copied a value out of "
        "CustomerLatents into data a production layer consumes. This is the D10 class "
        "of bug: it travels by data, not by import, so no static check will find it."
    )


def test_the_canary_is_present_in_the_latents_at_all():
    """A leak detector that is not actually planted detects nothing."""
    from antar.simulator.latents import CANARY_PREFIX, canary_for

    store = LatentStore(BASE, 20260822)
    latents = store.get("cust_000042")
    assert latents.canary == canary_for("cust_000042")
    assert CANARY_PREFIX in latents.canary
    assert CANARY_PREFIX in json.dumps(latents.as_row(), default=str)


def test_the_canary_is_per_customer():
    """A constant marker would not distinguish a leak from a coincidence."""
    from antar.simulator.latents import canary_for

    assert canary_for("cust_000001") != canary_for("cust_000002")


def test_no_canary_reaches_the_event_stream():
    from tests.statistical.helpers import batch as cached_batch

    batch = cached_batch()
    for event in batch.events[:300]:
        assert_no_canary(event.model_dump(mode="json"), context=f"AtRiskEvent {event.event_id}")


def test_no_canary_reaches_the_customer_context():
    from tests.statistical.helpers import batch as cached_batch

    batch = cached_batch()
    for customer_id, context in list(batch.customers.items())[:300]:
        assert_no_canary(context.model_dump(mode="json"), context=f"CustomerContext {customer_id}")


def test_no_canary_reaches_a_diagnosis():
    """L2's output is L3's input, so a marker here would reach the uplift models."""
    from antar.detect.pipeline import run_detection
    from tests.statistical.helpers import batch as cached_batch

    batch = cached_batch()
    result = run_detection(batch, batch.events[:150])
    for diagnosis in result.diagnoses:
        assert_no_canary(
            diagnosis.model_dump(mode="json"), context=f"Diagnosis {diagnosis.event_id}"
        )


def test_no_canary_reaches_the_detection_feature_frame():
    """The frame handed to LightGBM. The exact place D10's cousin would land."""
    from antar.detect.classifier import build_frame
    from tests.statistical.helpers import batch as cached_batch

    batch = cached_batch()
    frame = build_frame(batch.events[:200])
    assert_no_canary(frame.astype(str).to_dict(orient="records"), context="detection feature frame")


def test_the_canary_check_actually_catches_a_leak():
    """A guard that cannot fail is not a guard.

    Simulates the D10 mistake - a production payload carrying a value copied out of
    the answer key - and asserts the check fires.
    """
    from antar.simulator.latents import canary_for

    leaked = {"event_id": "evt_1", "amount_paise": 49900, "notes": canary_for("cust_000001")}
    with pytest.raises(AssertionError, match="canary reachable"):
        assert_no_canary(leaked, context="deliberately leaked payload")


def test_every_production_module_actually_imports():
    """A leak check that skips a module because it fails to import proves nothing."""
    failures: list[str] = []
    for layer in PRODUCTION_LAYERS:
        package = importlib.import_module(f"antar.{layer}")
        for info in pkgutil.walk_packages(package.__path__, prefix=f"antar.{layer}."):
            try:
                importlib.import_module(info.name)
            except Exception as exc:  # pragma: no cover - diagnostic path
                failures.append(f"{info.name}: {exc}")
    assert not failures, "\n".join(failures)
