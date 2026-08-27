"""The operator console, and the demo surface.

    python tasks.py console

PLAN.md M9 specifies three operator views:

> Console: three views — batch overview, decision trace explorer, shadow-price panel.
>
> **Acceptance:** Console answers *"why did you contact this customer at 11:04 on a
> Tuesday?"* in one click with a complete trace.

Those three are still here, unchanged, under **Operator console**. Everything else in
the sidebar is navigation added so that a walkthrough of this project never has to leave
the browser: the figures, the regulation register, the claims registry, every committed
document, and every artifact `python tasks.py evaluate` writes.

## Everything on screen comes from an artifact, the ledger, or a committed file

There is no computation in this file beyond formatting and arithmetic on values already
recorded. The batch overview reads `artifacts/batch_*.json`, the trace explorer reads the
hash-chained ledger through `audit/trace.py`, the shadow-price panel reads
`artifacts/allocation_*.json`, and the regulation screen reads `policy/regulations.py` —
the same list the constraint compiler turns into LP rows, not a copy of it.

That is not laziness. A console that recomputes is a second implementation of the
decision path, and when the two disagree the operator has no way to know which one is
lying. This one can only show what was recorded, which means a screenshot of it is
evidence. `tests/unit/test_console_contract.py` enforces it.

## No number in this file is typed

Every figure on every screen is read out of an artifact at render time. Where a screen
states a comparison — "49% more money" — the percentage is computed here from two
artifact values rather than written down, because a typed number is a number that can go
stale without anything noticing. That is POSTMORTEM D24 in one sentence.

## The verification banner is not decoration

Every ledger-backed screen checks `verify_chain()` and says so at the top. A trace from a
ledger that does not verify is worse than no trace, so the console refuses to look
authoritative about one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import streamlit as st

from antar.audit.ledger import Ledger
from antar.audit.trace import TraceIndex
from antar.config import artifacts_dir, repo_root
from antar.policy.regulations import REGULATIONS

st.set_page_config(page_title="Antar console", layout="wide")


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def available_ledgers() -> list[Path]:
    return sorted(artifacts_dir().glob("batch_*.db"))


@st.cache_resource
def open_ledger(path: str) -> Ledger:
    return Ledger(path)


def load_artifact(name: str) -> dict[str, Any] | None:
    path = artifacts_dir() / name
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def missing(name: str, how: str = "python tasks.py evaluate") -> None:
    """One phrasing for a missing artifact, everywhere.

    A fresh clone has no artifacts and that is correct — they are the output of a
    forty-minute job. The console says which command produces the file rather than
    rendering an empty panel that looks like a zero.
    """
    st.warning(f"`artifacts/{name}` is not present. Run `{how}`.")


def read_text(relative: str) -> str | None:
    path = repo_root() / relative
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def rupees(paise: Any) -> str:
    try:
        return f"Rs {float(paise) / 100:,.2f}"
    except (TypeError, ValueError):
        return "n/a"


URL = re.compile(r"""https://[^\s"'<>]+[^\s"'<>.,;:)\]]""")
"""URL-shaped text in a rendered message body.

The trailing character class excludes sentence punctuation, so a full stop after a
link stays outside the code span and the sentence still reads as a sentence.
"""

UNRESOLVABLE_URL = re.compile(r"""https://[^\s"'<>]*\.example(?:/[^\s"'<>]*)?[^\s"'<>.,;:)\]]?""")
"""The same, narrowed to hosts that are guaranteed never to resolve.

Used where a page also carries *real* citation links — the regulation register, the
committed documents — which must stay clickable. Defusing those would be a different
and worse defect than the one being fixed.
"""


def defuse_links(text: str) -> str:
    """Render URLs as inline code so the browser does not offer them as links.

    The message bodies contain `https://pay.antar.example/...`, which is unresolvable by
    design - `.example` is reserved by RFC 2606 and the template validator permits no
    other host. Streamlit auto-links anything URL-shaped, so a reader clicks one, gets
    DNS_PROBE_FINISHED_NXDOMAIN, and reasonably concludes something is broken.

    The paragraph is a human-readable summary; the exact string is still in the raw
    ledger entries below it, unmodified. This changes presentation only.
    """
    return URL.sub(lambda match: f"`{match.group(0)}`", text)


def defuse_example_links(text: str) -> str:
    """`defuse_links` for prose that also cites real sources.

    README section 12 quotes a drafted message verbatim, including its
    `pay.antar.example` link. The regulation register cites rbi.org.in and trai.gov.in,
    which a reader should be able to follow. So only the unresolvable host is defused.
    """
    return UNRESOLVABLE_URL.sub(lambda match: f"`{match.group(0)}`", text)


RFC_2606_NOTE = (
    "The `*.antar.example` links are **deliberately unresolvable**. `.example` is "
    "reserved by RFC 2606 precisely so it can never exist, and the `url` slot "
    "validator in `act/templates/` accepts no other host — a drafted message "
    "cannot contain a link that reaches anything. They are shown as text rather "
    "than links so nobody clicks one and reads DNS failure as a broken system."
)


# ---------------------------------------------------------------------------
# Banners
# ---------------------------------------------------------------------------


def simulated_banner() -> None:
    """N6, on every screen. A number on a dashboard travels further than its caveat."""
    st.caption(
        "Every figure here is **simulated** - generated by the model described in "
        "`docs/SIMULATOR_CARD.md`, calibrated against published Indian "
        "recurring-payments figures. None of it is a measured recovery rate from a live "
        "merchant."
    )


def chain_banner(ledger: Ledger) -> bool:
    result = ledger.verify_chain()
    if result.ok:
        st.success(
            f"Ledger verifies: {result.entries_checked} entries, head `{ledger.head()[:24]}...`"
        )
        return True
    st.error(
        f"**The ledger does not verify.** {len(result.breaks)} chain break(s), "
        f"first at seq {result.first_break.seq}: {result.first_break.detail} "
        "Treat everything below as unreliable."
    )
    return False


# ---------------------------------------------------------------------------
# Shared panels
#
# These carry no banners. The screens that compose them do, which is what keeps the
# simulated caveat on every screen without stacking three copies of it on the ones
# that reuse a panel.
# ---------------------------------------------------------------------------


def figure(name: str, caption: str = "") -> bool:
    """One of the five evaluation figures, full width."""
    path = artifacts_dir() / "figures" / name
    if not path.exists():
        st.warning(f"`artifacts/figures/{name}` is not present. Run `python tasks.py evaluate`.")
        return False
    st.image(str(path), use_container_width=True)
    if caption:
        st.caption(caption)
    return True


def limitation(number: str) -> str | None:
    """The text of one numbered limitation, lifted from the document itself.

    Paraphrasing a limitation on a slide is how a limitation becomes softer than the
    document that records it. This shows the document.
    """
    text = read_text("docs/LIMITATIONS.md")
    if text is None:
        return None
    pattern = re.compile(rf"^## {re.escape(number)} .*?(?=^## |\Z)", re.M | re.S)
    match = pattern.search(text)
    return match.group(0).strip() if match else None


def panel_batch_metrics(summary: dict[str, Any]) -> None:
    columns = st.columns(5)
    columns[0].metric("At-risk events", summary["events"])
    columns[1].metric("Contacted", summary["contacted"])
    columns[2].metric("Abstained", summary["abstained"])
    columns[3].metric("Holdout", summary["control"])
    columns[4].metric("Refused by gate", summary["refused"])

    st.markdown(
        "**Abstention is the product.** A recovery system that contacts everyone is a "
        "spam cannon with a dashboard; the interesting number above is the second-"
        "largest one."
    )

    columns = st.columns(4)
    columns[0].metric("Simulated recoveries", summary["recovered"])
    columns[1].metric("Opt-outs induced", summary["optouts"])
    columns[2].metric("Recovered", f"Rs {summary['recovered_rupees']:,.0f}")
    columns[3].metric("Net of cost", f"Rs {summary['net_rupees']:,.0f}")


def panel_provenance(summary: dict[str, Any]) -> None:
    st.subheader("Provenance")
    left, right = st.columns(2)
    left.write(
        {
            "uplift model": summary["model_version"],
            "policy version": summary["policy_version"],
            "seed": summary["seed"],
        }
    )
    right.write(
        {
            "ledger entries": summary["ledger_entries"],
            "chain verified": summary.get("chain_verified"),
            "replay self-consistent": summary.get("replay_self_consistent"),
            "ledger head": summary["ledger_head"][:32] + "...",
        }
    )
    if summary.get("notes"):
        st.info("\n\n".join(summary["notes"]))


def panel_event_kinds(ledger: Ledger) -> None:
    st.subheader("Where the events went")
    kinds: dict[str, int] = {}
    for entry in ledger.entries():
        kinds[entry.kind.value] = kinds.get(entry.kind.value, 0) + 1
    st.bar_chart(kinds)


def panel_trace_explorer(ledger: Ledger, verified: bool) -> None:
    """The acceptance criterion for M9: one click, one complete answer."""
    index = TraceIndex(ledger)
    event_ids = index.event_ids()
    if not event_ids:
        st.warning("This ledger has no events.")
        return

    filter_choice = st.radio(
        "Show",
        ["Contacted", "Abstained", "Holdout", "Refused", "All"],
        horizontal=True,
        index=0,
    )

    # One pass over the ledger for the whole batch, not one per event (D26).
    traces = {eid: index.trace(eid) for eid in event_ids}

    def matches(trace: Any) -> bool:
        decision = trace.decision or {}
        if filter_choice == "Contacted":
            return trace.approved
        if filter_choice == "Refused":
            return bool(trace.refusals)
        if filter_choice == "Holdout":
            return bool(decision.get("is_control"))
        if filter_choice == "Abstained":
            return not trace.actions and not decision.get("is_control")
        return True

    candidates = [eid for eid in event_ids if matches(traces[eid])]
    if not candidates:
        st.info(f"No events in this batch are **{filter_choice.lower()}**.")
        return

    event_id = st.selectbox(f"{len(candidates)} event(s)", candidates)
    trace = traces[event_id]

    st.subheader("The answer, in one paragraph")
    st.write(defuse_links(trace.narrate()))
    if "antar.example" in trace.narrate():
        st.caption(RFC_2606_NOTE)
    if not verified:
        st.warning(
            "The narrative above is assembled from a ledger that does not verify. It "
            "is what the file says, not what necessarily happened."
        )

    st.subheader("The evidence it was assembled from")
    left, right = st.columns(2)

    with left:
        st.markdown("**Event (L1)**")
        st.json(trace.event or {"missing": True})
        st.markdown("**Diagnosis (L2)**")
        st.json(trace.diagnosis or {"missing": True})

    with right:
        st.markdown("**Decision (L3)**")
        st.json(trace.decision or {"missing": True})
        st.markdown("**Actions (L4)**")
        st.json(list(trace.actions) or {"missing": True})
        st.markdown("**Outcome**")
        st.json(trace.outcome or {"missing": True})

    st.subheader("Reproducibility")
    st.write(trace.versions or {"note": "no versions recorded"})

    money = trace.money
    columns = st.columns(4)
    columns[0].metric("Amount at risk", rupees(money["amount_paise"]))
    columns[1].metric("Expected incremental", rupees(money["expected_incremental_paise"]))
    columns[2].metric("Cost incurred", rupees(money["cost_paise"] + money["discount_paise"]))
    columns[3].metric("Recovered", rupees(money["recovered_paise"]))

    if trace.gaps:
        st.info(
            "No ledger entry exists for: " + ", ".join(trace.gaps) + ". That is "
            "reported rather than inferred - most events legitimately end at "
            "'no action was worth taking'."
        )

    with st.expander("Raw ledger entries for this event"):
        st.json([entry.model_dump(mode="json") for entry in trace.entries])


def panel_policy_table(data: dict[str, Any]) -> None:
    st.subheader("Three policies, one contact capacity")
    st.dataframe(data["policies"], use_container_width=True)
    st.caption(
        "Per-1,000 figures use **at-risk cycles** as the denominator, not "
        "candidates. See POSTMORTEM D27."
    )

    delta = data["antar_minus_propensity_per_1000_rupees"]
    st.metric("Antar minus propensity targeting, per 1,000 at-risk cycles", f"Rs {delta:,.0f}")
    st.caption(
        "Propensity targeting is the comparison that matters. Beating 'contact "
        "everyone' is easy and proves nothing."
    )


def panel_constraint_prices(data: dict[str, Any]) -> None:
    prices = data.get("shadow_prices") or {}
    duals = prices.get("lp_duals") or []
    counterfactuals = prices.get("counterfactual_prices") or []
    slack = prices.get("slack_rows") or []

    st.subheader("Constraint prices")
    if duals or counterfactuals:
        st.dataframe([*duals, *counterfactuals], use_container_width=True)
    else:
        st.info("No priced constraints in this artifact.")

    if slack:
        st.info(
            f"**Non-binding rows: {', '.join(slack)}.** A zero price on contact "
            "capacity is a finding, not a null result. At this scenario's opt-out "
            "sensitivity the scarce resource is customer tolerance, not outbound "
            "capacity - Antar declines slots it is entitled to use, so one more slot "
            "is worth nothing. See `docs/LIMITATIONS.md` L14."
        )

    if prices.get("framing"):
        st.markdown(f"> {prices['framing']}")


def panel_retention(data: dict[str, Any]) -> None:
    verdicts = data.get("retention") or []
    if not verdicts:
        return
    st.subheader("Component retention")
    st.caption(
        "The rule was pre-registered in `docs/EVALUATION.md` 12.2 before the "
        "allocator existed, and ambiguity resolves to DELETE. Both components below "
        "were deleted on it."
    )
    for row in verdicts:
        st.markdown(
            f"**{row['component']}** - {'KEEP' if row.get('keep') else 'DELETE'}  \n"
            f"{row.get('rationale', '')}"
        )


def policy_row(data: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((row for row in data["policies"] if row["policy"] == name), None)


# ---------------------------------------------------------------------------
# The three operator views PLAN.md M9 specifies
# ---------------------------------------------------------------------------


def view_batch_overview(ledger: Ledger, scenario: str) -> None:
    st.header("Batch overview")
    simulated_banner()
    chain_banner(ledger)

    summary = load_artifact(f"batch_{scenario}.json")
    if summary is None:
        missing(f"batch_{scenario}.json", f"python tasks.py simulate SCENARIO={scenario}")
        return

    panel_batch_metrics(summary)
    panel_provenance(summary)
    panel_event_kinds(ledger)


def view_trace_explorer(ledger: Ledger) -> None:
    st.header("Why did you contact this customer?")
    simulated_banner()
    verified = chain_banner(ledger)
    panel_trace_explorer(ledger, verified)


def view_shadow_prices(scenario: str) -> None:
    st.header("What each constraint costs")
    simulated_banner()

    data = load_artifact(f"allocation_{scenario}.json")
    if data is None:
        missing(f"allocation_{scenario}.json")
        return

    panel_policy_table(data)
    panel_constraint_prices(data)
    panel_retention(data)


# ---------------------------------------------------------------------------
# Walkthrough
#
# The same evidence, ordered as an argument rather than as an operator's toolkit:
# problem, result, mechanism, audit, where it holds, whether it was tuned, what was
# withdrawn, what has never run.
# ---------------------------------------------------------------------------


def screen_problem() -> None:
    st.header("1 · Why rupees recovered is the wrong metric")
    simulated_banner()

    st.markdown(
        """
Indian merchants running recurring billing lose money when auto-debits fail. The
standard response is a retry schedule and dunning messages, and the standard metric is
**rupees recovered**. That metric is close to useless, for two reasons.

**A large share of failed debits recover on their own.** A system reporting gross
recovery is mostly taking credit for self-healing. The only honest denominator is the
*incremental* rupees against a randomised control — which is why this project holds one
back and never trains on it.

**The notification is also a cancellation prompt.** The RBI e-mandate framework requires
a pre-transaction notification 24 hours before every debit, carrying an opt-out. So
retrying harder does not merely waste attempts; it manufactures churn. Contacting is not
free upside, and a ranker that sorts by "likely to pay" targets the quietly-lapsed
enthusiastically, because they look likely to pay too.
        """
    )

    st.subheader("The regulations, as data")
    st.caption(
        "Read live from `antar/policy/regulations.py` — the same list the constraint "
        "compiler turns into LP rows, not a copy of it. N5: every rule carries a "
        "citation to a primary source and a verification level, and a rule resting on "
        "a secondary source is not permitted to block money."
    )
    rows = [
        {
            "id": rule.id,
            "title": rule.title,
            "severity": rule.severity.value,
            "verified": rule.verification.value,
            "effective": str(rule.effective_date),
            "source": rule.citation_url,
        }
        for rule in REGULATIONS
    ]
    st.dataframe(rows, use_container_width=True)
    st.caption(
        f"{len(REGULATIONS)} rules. Verifying them against primary sources found six "
        "errors in this project's own plan — the commercial-contact window opens at "
        "10:00, not the 09:00 the plan assumed, because TCCCPR Schedule II para 3(1) "
        "Note-1 makes the 08:00-10:00 band default-off. That correction cost an hour of "
        "daily capacity and raised our own shadow prices. See `docs/REGULATORY_REGISTER.md`."
    )

    with st.expander("The full register, with quotes and discrepancies"):
        text = read_text("docs/REGULATORY_REGISTER.md")
        st.markdown(defuse_example_links(text) if text else "_Not present._")


def screen_result(scenario: str) -> None:
    st.header("2 · The result, with no assumption about what churn costs")
    simulated_banner()

    data = load_artifact(f"allocation_{scenario}.json")
    if data is None:
        missing(f"allocation_{scenario}.json")
        return

    figure(
        "01_three_policies.png",
        "Contact everyone, propensity targeting, and Antar — on the same batch under "
        "the same contact capacity.",
    )

    antar = policy_row(data, "antar")
    ranker = policy_row(data, "propensity")
    if antar is None or ranker is None:
        st.warning("This allocation artifact does not contain both policies to compare.")
        return

    left, right = st.columns(2)
    left.metric(
        "Antar — incremental per 1,000 at-risk cycles",
        f"Rs {antar['incremental_per_1000_at_risk_rupees']:,.0f}",
        f"{antar['contacts']:,} contacts",
    )
    right.metric(
        "Propensity targeting — same, same capacity",
        f"Rs {ranker['incremental_per_1000_at_risk_rupees']:,.0f}",
        f"{ranker['contacts']:,} contacts",
    )

    # Computed, not typed. A percentage written into prose is a percentage that goes
    # stale silently when the evaluation is re-run (POSTMORTEM D24).
    more_money = (
        antar["incremental_per_1000_at_risk_rupees"] / ranker["incremental_per_1000_at_risk_rupees"]
        - 1
    )
    less_outreach = 1 - antar["contacts"] / ranker["contacts"]
    st.success(
        f"**{more_money:.0%} more money on {less_outreach:.0%} less outreach.** "
        "Both halves of that sentence are a contact count and a rupee figure. Neither "
        "depends on how you price a cancellation."
    )

    st.subheader("The full table")
    panel_policy_table(data)

    st.caption(
        "The net column is where the pricing assumption lives — it values an induced "
        "cancellation at a multiplier of the cycle, which is an assumption, and it is "
        "why the headline above is stated on the incremental column instead."
    )


def screen_architecture(scenario: str) -> None:
    st.header("3 · Five layers, and the two with no language model in them")
    simulated_banner()

    path = repo_root() / "docs" / "img" / "architecture.png"
    if path.exists():
        st.image(str(path), use_container_width=True)
    else:
        st.warning(
            "`docs/img/architecture.png` is not present. Run "
            "`python scripts/make_architecture_diagram.py`."
        )

    st.markdown(
        """
**Detection (L2) and decision (L3) contain no language model at all, and that is
deliberate.** A language model deciding whether to debit ₹4,000 is an unbounded action
with no confidence interval and no counterfactual. Failure classification is a table plus
a gradient-boosted model. Allocation is a linear programme whose constraint rows are the
actual regulations.

The model writes one thing: the customer-facing sentence, into a registered template
slot. It never picks an amount, a date, a discount, a URL, or whether to contact at all.
That factoring is enforced by tests that patch the API client to raise and then run the
full decision path — if any decision depended on the model, the suite would fail rather
than degrade.
        """
    )

    figure(
        "05_batch_funnel.png",
        "Where a batch of at-risk events actually goes. The gate sits between the "
        "decision and the action, and every money-moving function is discovered by "
        "introspection rather than listed — see `tests/unit/test_gate_coverage.py`.",
    )

    summary = load_artifact(f"batch_{scenario}.json")
    if summary is not None:
        panel_batch_metrics(summary)


def screen_trace(ledger: Ledger) -> None:
    st.header("4 · One click, one complete answer")
    simulated_banner()
    verified = chain_banner(ledger)
    st.caption(
        "Every money action passes one gate and lands in a hash-chained ledger. The "
        "acceptance criterion for this screen, from PLAN.md M9, is that it answers "
        "*“why did you contact this customer at 11:04 on a Tuesday?”* in one "
        "click with a complete trace."
    )
    panel_trace_explorer(ledger, verified)


def screen_roundtrip() -> None:
    st.header("Razorpay test-mode round trip")
    simulated_banner()

    roundtrip = load_artifact("razorpay_roundtrip.json")
    if roundtrip is None:
        missing("razorpay_roundtrip.json", "python tasks.py roundtrip")
        st.caption(
            "It needs `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` for a **test-mode** key. "
            "`scripts/record_roundtrip.py` refuses to run against a live key."
        )
        return

    st.markdown(
        """
The gate rests on an assumption: that replaying an idempotency key does not
double-charge. That was checked against the live sandbox rather than assumed.
        """
    )
    columns = st.columns(4)
    columns[0].metric("Calls", roundtrip["calls"])
    columns[1].metric("Succeeded", roundtrip["succeeded"])
    columns[2].metric("Mode", roundtrip["mode"])
    columns[3].metric("Key", roundtrip["key_id_prefix"])
    st.caption(
        "The one non-success is a deliberate 400, recorded to capture the real error "
        "envelope rather than a guessed one."
    )
    st.info(roundtrip["note"])

    st.subheader("What each call returned")
    st.caption("Field names and shapes only — no amounts, no contact details, ids truncated.")
    st.json(roundtrip["records"])

    st.markdown(
        "Running this found two calls in our own script written against signatures that "
        "do not exist, and a Razorpay validation rule that appears in no documentation "
        "we had read. Neither was reachable by any test."
    )

    text = limitation("L19")
    if text:
        st.subheader("And what it does not show")
        st.markdown(text)


def screen_regimes(scenario: str) -> None:
    st.header("5 · Two regimes, and where this system is the wrong answer")
    simulated_banner()

    phase = load_artifact("phase_diagram.json")

    # The cell count is read, not typed. It was typed once, as "264", and stayed that
    # way after the grid was regenerated without its post-hoc extension - the same
    # defect this screen exists to talk about.
    shape = ""
    if phase is not None:
        axes = phase["summary"]["axes"]
        shape = (
            f"{len(phase['cells']):,} cells: {len(axes['x_mean_self_heal'])} self-heal "
            f"levels x {len(axes['y_mean_optout_sensitivity'])} opt-out levels x "
            f"{len(phase['summary']['panels'])} channel-mix panels. "
        )
    figure(
        "02_phase_diagram.png",
        shape + "The axes are mean self-heal rate and mean opt-out sensitivity — the "
        "two parameters the result is most sensitive to.",
    )

    if phase is not None:
        panels = phase["summary"]["panels"]
        columns = st.columns(len(panels))
        for column, (name, panel) in zip(columns, panels.items(), strict=False):
            ceiling = panel.get("capacity_binding_ceiling_optout")
            column.metric(
                name,
                f"{panel['antar_wins_share']:.0%} of cells",
                f"capacity binds in {panel['capacity_binds_share']:.0%}"
                + (f", only below opt-out {ceiling}" if ceiling is not None else ""),
            )
        st.markdown(f"> {phase['summary']['interpretation']}")
        st.caption(f"Metric: {phase['summary']['metric']}")
    else:
        missing("phase_diagram.json")

    st.markdown(
        """
**This is the most useful thing in the project.** Contact capacity binds only at the low
end of the opt-out range. Above it every capacity shadow price is zero — because Antar
declines slots it is legally entitled to use.

The same merchant is running two different businesses depending on which side of that
line they sit. On one side outbound capacity is scarce and you want to expand it. On the
other, customer tolerance is scarce and expanding capacity makes things worse. Antar is
the wrong answer in one of those regimes, and the diagram is how you tell which one you
are in.
        """
    )

    data = load_artifact(f"allocation_{scenario}.json")
    if data is None:
        missing(f"allocation_{scenario}.json")
        return
    panel_constraint_prices(data)
    panel_retention(data)

    text = limitation("L14")
    if text:
        with st.expander("L14 — every capacity shadow price is zero in the base scenario"):
            st.markdown(text)


def screen_preregistration(scenario: str) -> None:
    st.header("6 · Was the headline tuned? Every defensible choice, run")
    simulated_banner()

    figure(
        "03_specification_curve.png",
        "Every defensible analytic choice, run. The pre-registered specification is marked.",
    )

    curve = load_artifact(f"specification_curve_{scenario}.json")
    if curve is None:
        missing(f"specification_curve_{scenario}.json")
        return

    stats = curve["summary"]
    percentile = stats["pre_registered_percentile"]
    columns = st.columns(4)
    columns[0].metric("Specifications", f"{stats['n_specifications']:,}")
    columns[1].metric("Median share", f"{stats['median']:.2%}")
    columns[2].metric("Clearing the bar", f"{stats['fraction_clearing_threshold']:.0%}")
    columns[3].metric("Pre-registered sits at", f"{percentile:.1f}th pct")

    if percentile is None:
        st.warning(
            "The pre-registered specification is not in the evaluated set, so its "
            "percentile is unknown. That is reported rather than rendered as a "
            "number — see POSTMORTEM D35."
        )
    else:
        st.success(
            f"**The registered specification is less favourable to our own finding "
            f"than {100 - percentile:.0f}% of the alternatives**, and it was committed "
            "before any of them were run. The git history shows the order."
        )

    st.subheader("Which choices actually moved the answer")
    st.caption("Spread of level means, per dimension. Larger means the choice mattered more.")
    st.bar_chart(stats["variance_by_dimension"])

    st.subheader("The specification that was registered, in advance")
    st.json(curve["pre_registered"])
    st.markdown(f"> {curve['verdict']}")


def screen_withdrawal() -> None:
    st.header("7 · The claim that did not survive its own test")
    simulated_banner()

    claims = load_artifact("claims.json")
    if claims is None:
        missing("claims.json")
        return

    for name, verdict in claims.items():
        supported = verdict["supported"]
        (st.success if supported else st.error)(
            f"**{name}** — {'SUPPORTED' if supported else 'WITHDRAWN'}"
        )
        st.markdown(verdict["detail"])

        evidence = verdict.get("evidence") or {}
        shares = evidence.get("share_by_scenario")
        if shares:
            threshold = evidence.get("threshold")
            st.caption(
                f"Negative-uplift share by scenario, against a pre-registered bar of "
                f"{threshold:.0%} in at least "
                f"{evidence.get('min_qualifying_scenarios')} of 3."
            )
            st.dataframe(
                [
                    {
                        "scenario": key,
                        "share": value,
                        "clears the bar": value >= threshold if threshold else None,
                    }
                    for key, value in shares.items()
                ],
                use_container_width=True,
            )
        with st.expander(f"Full evidence for {name}"):
            st.json(verdict)

    st.markdown(
        """
This project's motivating claim was that a meaningful population of customers has
**negative uplift** — that contacting them costs money, because the notification prompts
a cancellation.

A reviewer found that the simulator discounted treated recovery by the opt-out hazard and
did not discount the control arm. An asymmetry, undocumented, biased toward our own
thesis. Worse, it had been logged as known-and-unfixed on the reasoning that correcting
it "would move numbers in Antar's favour" — which was **backwards**, and that error
protected the defect across three review cycles.

The correction was pre-registered, naming withdrawal as a possible outcome, and then run.
The claim stopped clearing the bar registered for it, so **it is withdrawn**. A test now
fails the build if any document asserts it, and separately fails if the README drops it
silently — because deleting a claim quietly is worse than the failure.

**What survived is the result on screen 2**, which needs no assumption about cancellation
pricing at all.
        """
    )

    st.caption(
        "Enforced by `tests/statistical/test_withdrawn_claims_are_not_stated.py`, which "
        "reads the adjudicated verdict above rather than a list someone maintains."
    )


def screen_postmortem() -> None:
    st.header("The defect log")
    simulated_banner()

    text = read_text("docs/POSTMORTEM.md")
    if text is None:
        st.warning("`docs/POSTMORTEM.md` is not present.")
        return

    entries = re.findall(r"^## (D\d+)[^\n]*", text, re.M)
    st.caption(
        f"{len(entries)} entries, each with root cause, fix, and order of discovery. "
        "The ones worth reading are the defects in the safeguards: a retention rule that "
        "became a ratchet (D25), a headline holding a different quantity in the wrong "
        "unit for two milestones (D24), an ablation measuring a component the decision "
        "layer could not see (D16)."
    )

    query = st.text_input("Filter by entry or keyword", "")
    if query:
        lines = [line for line in text.splitlines() if query.lower() in line.lower()]
        st.caption(f"{len(lines)} matching line(s).")
        st.code("\n".join(lines) or "no match", language="markdown")
    else:
        st.markdown(defuse_example_links(text))


def screen_not_run() -> None:
    st.header("8 · What has never run")
    simulated_banner()

    text = limitation("L19")
    if text:
        st.markdown(text)
    else:
        st.warning("`docs/LIMITATIONS.md` is not present.")

    st.info(
        "`charge_mandate` has never executed. It needs an authenticated e-mandate that "
        "no script can create unattended, so the endpoint this entire thesis is about is "
        "still fixtures only. That is in the README's opening block, not a footnote."
    )

    evaluation = load_artifact("evaluation.json")
    if evaluation is not None:
        st.subheader("What did run, and how long it took")
        st.dataframe(evaluation["stages"], use_container_width=True)
        st.caption(
            f"scenario `{evaluation['scenario']}`, seed {evaluation['seed']}, "
            f"quick={evaluation['quick']}. Failed stages: "
            f"{evaluation['failed'] or 'none'}."
        )

    with st.expander("Every limitation, in full"):
        full = read_text("docs/LIMITATIONS.md")
        st.markdown(defuse_example_links(full) if full else "_Not present._")


# ---------------------------------------------------------------------------
# Reference
# ---------------------------------------------------------------------------


FIGURE_CAPTIONS = {
    "01_three_policies.png": "Contact everyone, propensity targeting, Antar.",
    "02_phase_diagram.png": "264 configurations; where the advantage holds.",
    "03_specification_curve.png": "540 analytic choices, with the registered one marked.",
    "04_detection.png": "Failure-cause classification, per class.",
    "05_batch_funnel.png": "Where a batch of at-risk events goes.",
}


def screen_figures() -> None:
    st.header("Figures")
    simulated_banner()

    paths = sorted((artifacts_dir() / "figures").glob("*.png"))
    if not paths:
        missing("figures/")
        return
    for path in paths:
        st.subheader(path.name)
        st.image(str(path), use_container_width=True)
        caption = FIGURE_CAPTIONS.get(path.name)
        if caption:
            st.caption(caption)
        st.divider()


def screen_documents() -> None:
    st.header("Documents")
    simulated_banner()

    candidates = ["README.md", "artifacts/RESULTS.md"]
    candidates += sorted(f"docs/{path.name}" for path in (repo_root() / "docs").glob("*.md"))
    present = [name for name in candidates if (repo_root() / name).exists()]
    if not present:
        st.warning("No documents found.")
        return

    chosen = st.selectbox("Document", present)
    text = read_text(chosen)
    if text is None:
        st.warning(f"`{chosen}` disappeared.")
        return

    st.caption(
        f"`{chosen}` — {len(text.splitlines()):,} lines. Rendered from the file on "
        "disk, so what you see is what is committed."
    )
    if ".example" in text:
        st.caption(RFC_2606_NOTE)
    st.markdown(defuse_example_links(text))


def screen_artifacts() -> None:
    st.header("Artifacts")
    simulated_banner()

    paths = sorted(artifacts_dir().glob("*.json"))
    if not paths:
        missing("*.json")
        return

    st.caption(
        "Everything `python tasks.py evaluate` writes. Every number on every other "
        "screen is a value from one of these, or a rounding or unit conversion of one — "
        "`tests/statistical/test_results_are_reproducible.py` fails the build otherwise."
    )
    chosen = st.selectbox("Artifact", paths, format_func=lambda p: p.name)
    payload = load_artifact(chosen.name)
    if payload is None:
        st.warning(f"`{chosen.name}` is not readable as JSON.")
        return
    st.caption(f"{chosen.stat().st_size:,} bytes")
    st.json(payload)


# ---------------------------------------------------------------------------
# Shell
# ---------------------------------------------------------------------------


WALKTHROUGH = [
    "1 · The problem",
    "2 · The result",
    "3 · Architecture",
    "4 · Decision trace",
    "5 · Two regimes",
    "6 · Pre-registration",
    "7 · The withdrawal",
    "8 · What has never run",
]

OPERATOR = ["Batch overview", "Decision trace", "Shadow prices"]

REFERENCE = ["Razorpay round trip", "Defect log", "Figures", "Documents", "Artifacts"]

SECTIONS = WALKTHROUGH + OPERATOR + REFERENCE

NEEDS_LEDGER = {"4 · Decision trace", "Batch overview", "Decision trace"}


def step(offset: int) -> None:
    """Move one screen along the sidebar order.

    Clicking a radio button mid-sentence is awkward on camera; a Next button is not.
    The radio and the buttons share `st.session_state["section"]`, so they cannot
    disagree about which screen is showing.
    """
    current = SECTIONS.index(st.session_state.get("section", SECTIONS[0]))
    st.session_state["section"] = SECTIONS[(current + offset) % len(SECTIONS)]


def render(section: str, ledger: Ledger | None, scenario: str) -> None:
    if section in NEEDS_LEDGER and ledger is None:
        st.warning("This screen reads the ledger. Select one in the sidebar to continue.")
        return

    if section == "1 · The problem":
        screen_problem()
    elif section == "2 · The result":
        screen_result(scenario)
    elif section == "3 · Architecture":
        screen_architecture(scenario)
    elif section == "4 · Decision trace":
        assert ledger is not None
        screen_trace(ledger)
    elif section == "5 · Two regimes":
        screen_regimes(scenario)
    elif section == "6 · Pre-registration":
        screen_preregistration(scenario)
    elif section == "7 · The withdrawal":
        screen_withdrawal()
    elif section == "8 · What has never run":
        screen_not_run()
    elif section == "Batch overview":
        assert ledger is not None
        view_batch_overview(ledger, scenario)
    elif section == "Decision trace":
        assert ledger is not None
        view_trace_explorer(ledger)
    elif section == "Shadow prices":
        view_shadow_prices(scenario)
    elif section == "Razorpay round trip":
        screen_roundtrip()
    elif section == "Defect log":
        screen_postmortem()
    elif section == "Figures":
        screen_figures()
    elif section == "Documents":
        screen_documents()
    elif section == "Artifacts":
        screen_artifacts()


def main() -> None:
    st.title("Antar")
    st.caption(
        "A causal revenue-recovery controller for Indian recurring payments. "
        "It decides *whether* to act, not just *how*."
    )

    ledgers = available_ledgers()
    with st.sidebar:
        st.header("Data")
        if not ledgers:
            st.error(
                "No ledger found in `artifacts/`.\n\nRun `python tasks.py simulate` to produce one."
            )
            chosen = None
            scenario = "base"
        else:
            chosen = st.selectbox("Ledger", ledgers, format_func=lambda p: p.name)
            scenario = chosen.stem.removeprefix("batch_")

        st.divider()
        st.radio(
            "Walkthrough",
            SECTIONS,
            key="section",
            captions=(
                ["the metric everyone reports is wrong"]
                + [""] * (len(WALKTHROUGH) - 1)
                + ["the three views PLAN.md M9 specifies"]
                + [""] * (len(OPERATOR) - 1)
                + ["everything else, in full"]
                + [""] * (len(REFERENCE) - 1)
            ),
        )

        left, right = st.columns(2)
        left.button("← Back", on_click=step, args=(-1,), use_container_width=True)
        right.button("Next →", on_click=step, args=(1,), use_container_width=True)

        st.divider()
        st.caption(
            "Nothing in this console recomputes a decision. It reads the "
            "hash-chained ledger, the evaluation artifacts, and the committed "
            "documents, so what you see is what was recorded."
        )

    section = st.session_state.get("section", SECTIONS[0])
    position = SECTIONS.index(section) + 1
    st.caption(f"{position} of {len(SECTIONS)} · `{section}`")

    ledger = open_ledger(str(chosen)) if chosen is not None else None
    render(section, ledger, scenario)


main()
