"""The decision layer: features, learners, metrics, OPE, stopping rules."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from antar.decide import ope
from antar.decide.stopping import StoppingPolicy, StopReason, abstained_value_paise, summarise
from antar.decide.uplift.base import UpliftEstimator, align, encode
from antar.decide.uplift.learners import ALL_MODELS, BASELINES, LEARNERS
from antar.decide.uplift.metrics import (
    abstention_value,
    auuc,
    calibration,
    qini_coefficient,
    sign_metrics,
    zscore,
)
from antar.signals.schemas import CustomerContext, MandateState

# --------------------------------------------------------------------- setup


def synthetic(n: int = 600, seed: int = 7):
    """A dataset with a known, heterogeneous, sometimes-negative treatment effect."""
    rng = np.random.default_rng(seed)
    X = pd.DataFrame(
        {
            "issuer": pd.Categorical(rng.choice(["HDFC", "ICICI", "SBI"], size=n)),
            "log_amount": rng.normal(10, 1, size=n),
            "tenure_months": rng.integers(1, 60, size=n).astype(float),
        }
    )
    # The effect is negative for short-tenure customers and positive for long ones.
    tau = np.where(X["tenure_months"] < 20, -0.15, 0.20)
    base = 0.30 + 0.02 * (X["log_amount"] - 10)
    treatment = rng.binomial(1, 0.5, size=n)
    p = np.clip(base + treatment * tau, 0.01, 0.99)
    outcome = rng.binomial(1, p)
    return X, treatment, outcome, tau


# ------------------------------------------------------------------ learners


@pytest.mark.parametrize("name", sorted(LEARNERS))
def test_every_learner_satisfies_the_protocol(name):
    assert isinstance(LEARNERS[name](), UpliftEstimator)


@pytest.mark.parametrize("name", sorted(ALL_MODELS))
def test_predicting_before_fitting_raises(name):
    """PLAN.md section 10: a missing model refuses to act rather than defaulting."""
    model = ALL_MODELS[name]()
    with pytest.raises(RuntimeError, match="not fitted"):
        model.predict_uplift(pd.DataFrame({"a": [1.0]}))


@pytest.mark.parametrize("name", ["t_learner", "x_learner", "r_learner"])
def test_learners_recover_a_planted_heterogeneous_effect(name):
    """The floor: if a learner cannot see an effect this obvious, nothing it says
    about a real one means anything."""
    X, treatment, outcome, _tau = synthetic()
    model = LEARNERS[name]().fit(X, treatment, outcome, propensity=0.5)
    predicted = model.predict_uplift(X)

    short = X["tenure_months"] < 20
    assert predicted[short].mean() < predicted[~short].mean(), (
        f"{name} failed to order the negative-effect group below the positive one"
    )


@pytest.mark.parametrize("name", ["t_learner", "x_learner", "r_learner"])
def test_learners_can_express_a_negative_uplift(name):
    """Not an edge case here - it is the population the product is built around."""
    X, treatment, outcome, _ = synthetic()
    predicted = LEARNERS[name]().fit(X, treatment, outcome, propensity=0.5).predict_uplift(X)
    assert (predicted < 0).any(), f"{name} never predicts a negative uplift"


def test_propensity_baseline_can_never_abstain():
    """The structural limitation of the sophisticated wrong answer.

    Its score is a probability in [0, 1], so it cannot represent harm and can never
    recommend staying silent. That is not a handicap we imposed.
    """
    X, treatment, outcome, _ = synthetic()
    predicted = BASELINES["propensity"]().fit(X, treatment, outcome).predict_uplift(X)
    assert (predicted >= 0).all()


def test_always_abstain_baseline_exists_and_is_negative():
    """Added because it embarrasses the learners on F1. See POSTMORTEM D15."""
    X, treatment, outcome, _ = synthetic()
    predicted = BASELINES["always_abstain"]().fit(X, treatment, outcome).predict_uplift(X)
    assert (predicted < 0).all()


def test_encoding_is_shared_so_the_bakeoff_compares_methods_not_encodings():
    X, _, _, _ = synthetic(50)
    encoded = encode(X)
    assert "issuer_HDFC" in encoded.columns
    assert encoded.dtypes.apply(lambda d: d.kind == "f").all()


def test_align_fills_absent_levels_rather_than_shifting_columns():
    reference = encode(synthetic(100, seed=1)[0])
    other = encode(pd.DataFrame({"issuer": pd.Categorical(["HDFC"]), "log_amount": [10.0], "tenure_months": [5.0]}))
    aligned = align(reference, other)
    assert list(aligned.columns) == list(reference.columns)
    assert aligned["issuer_SBI"].iloc[0] == 0.0


# ------------------------------------------------------------------- metrics


def test_a_perfect_ranker_beats_a_random_one_on_qini():
    _X, treatment, outcome, tau = synthetic()
    perfect = qini_coefficient(tau, treatment, outcome)
    rng = np.random.default_rng(0)
    noise = qini_coefficient(rng.normal(size=len(tau)), treatment, outcome)
    assert perfect > noise


def test_auuc_is_scale_free():
    """Normalised, so a learner scored on a bigger split is not flattered by size."""
    _X, treatment, outcome, tau = synthetic(400)
    small = auuc(tau[:200], treatment[:200], outcome[:200])
    large = auuc(tau, treatment, outcome)
    assert abs(small - large) < 0.6


def test_sign_metrics_on_a_perfect_predictor():
    truth = np.array([-0.2, -0.1, 0.1, 0.3])
    metrics = sign_metrics(truth, truth)
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.actually_negative == 2


def test_sign_metrics_on_a_constant_abstainer():
    """docs/POSTMORTEM.md D15: at prevalence p a constant predictor scores F1 =
    2p/(1+p), which is a floor every learner must clear and none of ours did."""
    truth = np.array([-0.2, 0.1, 0.1, 0.1, 0.1])  # prevalence 0.2
    metrics = sign_metrics(np.full(5, -1.0), truth)
    assert metrics.recall == 1.0
    assert metrics.precision == pytest.approx(0.2)
    assert metrics.f1 == pytest.approx(2 * 0.2 / 1.2, abs=1e-4)


def test_abstention_value_prices_magnitude_not_decisions():
    """A correct abstention on a large harmful cycle is worth more than one on a
    trivial cycle. Counting decisions would treat them as equal."""
    predicted = np.array([-1.0, -1.0])
    truth = np.array([-0.10, -0.10])
    small = abstention_value(predicted, truth, np.array([1000.0, 1000.0]))
    large = abstention_value(predicted, truth, np.array([100000.0, 100000.0]))
    assert large.saved_paise > small.saved_paise
    assert small.n_abstained == large.n_abstained == 2


def test_abstention_on_a_positive_customer_costs_money():
    value = abstention_value(np.array([-1.0]), np.array([0.20]), np.array([50000.0]))
    assert value.forgone_paise > 0
    assert value.net_paise < 0


def test_calibration_reports_bias_in_the_lowest_decile():
    predicted = np.linspace(-0.3, 0.3, 200)
    truth = predicted + 0.05
    report = calibration(predicted, truth)
    assert len(report.deciles) == 10
    assert report.negative_region_bias == pytest.approx(-0.05, abs=1e-6)


def test_zscore_handles_a_constant_candidate_set():
    assert zscore([0.5, 0.5, 0.5]) == [0.0, 0.0, 0.0]


# ----------------------------------------------------------------------- OPE


def test_known_propensities_give_an_unbiased_ips_estimate():
    rng = np.random.default_rng(3)
    n = 20000
    logged = rng.integers(0, 4, size=n)
    propensity = np.full(n, 0.25)
    reward = np.where(logged == 2, 1.0, 0.0)
    target = np.full(n, 2)
    result = ope.ips(reward, target, logged, propensity)
    assert result.estimate == pytest.approx(1.0, abs=0.05)
    assert result.reliable


def test_a_zero_propensity_is_a_bookkeeping_error_not_a_small_one():
    with pytest.raises(ValueError, match="bookkeeping error"):
        ope.ips(np.array([1.0]), np.array([1]), np.array([1]), np.array([0.0]))


def test_snips_is_bounded_by_the_reward_range_and_ips_is_not():
    rng = np.random.default_rng(5)
    n = 4000
    logged = rng.integers(0, 10, size=n)
    propensity = np.full(n, 0.1)
    reward = (logged == 3).astype(float)
    target = np.full(n, 3)
    assert ope.snips(reward, target, logged, propensity).estimate <= 1.0


def test_low_ess_is_reported_as_unreliable():
    """§7.2: an estimate with an ESS of 40 must not be quotable without the caveat."""
    n = 2000
    logged = np.zeros(n, dtype=int)
    logged[:3] = 1
    propensity = np.where(logged == 1, 0.001, 0.999)
    result = ope.ips(np.ones(n), np.ones(n, dtype=int), logged, propensity)
    assert not result.reliable
    assert "UNRELIABLE" in result.as_dict()["note"]


def test_weight_diagnostics_are_always_present():
    result = ope.ips(np.ones(10), np.ones(10, dtype=int), np.ones(10, dtype=int), np.full(10, 0.5))
    diagnostics = result.as_dict()["diagnostics"]
    for key in ("effective_sample_size", "max_weight", "p99_weight", "clipped_fraction"):
        assert key in diagnostics


def test_doubly_robust_reduces_to_the_outcome_model_when_nothing_matches():
    n = 100
    logged = np.zeros(n, dtype=int)
    target = np.ones(n, dtype=int)  # never agrees, so every weight is zero
    result = ope.doubly_robust(
        np.ones(n), target, logged, np.full(n, 0.5), np.full(n, 0.42), np.full(n, 0.11)
    )
    assert result.estimate == pytest.approx(0.42)


def test_the_consistency_check_flags_a_disagreement():
    comparison = ope.OPEComparison(on_policy=0.5, on_policy_ci=(0.45, 0.55))
    comparison.results["doubly_robust"] = ope.doubly_robust(
        np.ones(10), np.ones(10, dtype=int), np.ones(10, dtype=int), np.full(10, 0.5),
        np.full(10, 0.90), np.full(10, 0.90),
    )
    assert comparison.consistent is False
    assert "finding" in comparison.as_dict()["consistency_note"]


def test_the_consistency_check_is_none_without_a_comparison():
    assert ope.OPEComparison().consistent is None


# ------------------------------------------------------------------ stopping


def customer(**overrides) -> CustomerContext:
    base = {"customer_id": "cust_1", "merchant_id": "mer_1"}
    return CustomerContext(**{**base, **overrides})


def test_optout_stops_absolutely():
    decision = StoppingPolicy().evaluate(customer(optout_received=True))
    assert decision.stopped and decision.reason is StopReason.OPTOUT_RECEIVED


def test_a_negative_ci_stops_but_a_straddling_one_does_not():
    """The sleeping-dogs rule, and the asymmetry that makes it defensible.

    The whole interval below zero is evidence of harm. An interval straddling zero is
    ignorance, and the answer to ignorance is to let the budget-constrained allocator
    decide - not to abstain confidently.
    """
    policy = StoppingPolicy()
    assert policy.evaluate(customer(), uplift_ci=(-0.20, -0.02)).reason is (
        StopReason.NEGATIVE_UPLIFT_CI
    )
    assert not policy.evaluate(customer(), uplift_ci=(-0.20, 0.05)).stopped
    assert not policy.evaluate(customer(), uplift_ci=(0.01, 0.30)).stopped


def test_a_missing_model_stops_rather_than_targeting_everyone():
    """PLAN.md section 10, last row. The safe failure is to do nothing."""
    decision = StoppingPolicy().evaluate(customer(), has_model=False)
    assert decision.stopped and decision.reason is StopReason.NO_UPLIFT_MODEL


def test_revoked_and_paused_mandates_stop_for_different_reasons():
    policy = StoppingPolicy()
    assert policy.evaluate(customer(mandate_state=MandateState.REVOKED)).reason is (
        StopReason.MANDATE_REVOKED
    )
    assert policy.evaluate(customer(mandate_state=MandateState.PAUSED)).reason is (
        StopReason.MANDATE_PAUSED
    )


def test_budget_and_attempt_caps_stop():
    policy = StoppingPolicy(max_attempts_per_event=2, contacts_per_30d=3)
    assert policy.evaluate(customer(attempts_on_event=2)).reason is StopReason.ATTEMPT_CAP
    assert policy.evaluate(customer(contacts_in_window=3)).reason is (
        StopReason.CONTACT_BUDGET_EXHAUSTED
    )


def test_ordering_records_the_most_final_reason():
    """A customer who both opted out and exhausted their budget is recorded as having
    opted out, because that is the fact a human reading the trace needs."""
    decision = StoppingPolicy().evaluate(customer(optout_received=True, contacts_in_window=99))
    assert decision.reason is StopReason.OPTOUT_RECEIVED


def test_every_stop_reason_has_a_human_explanation():
    policy = StoppingPolicy()
    for reason in StopReason:
        from antar.decide.stopping import HUMAN_READABLE

        assert HUMAN_READABLE[reason], reason
    assert policy.evaluate(customer()).explanation == ""


def test_money_earned_by_not_acting_is_quantifiable():
    """PLAN.md M5: rupees earned by NOT acting, reported separately from rupees earned
    by acting."""
    from antar.decide.stopping import StopDecision

    stopped = StopDecision(True, StopReason.NEGATIVE_UPLIFT_CI)
    value = abstained_value_paise([stopped], [100_000], [-0.10], optout_loss_multiplier=6.0)
    assert value == 60_000


def test_stopping_summary_counts_by_reason():
    policy = StoppingPolicy()
    decisions = [
        policy.evaluate(customer(optout_received=True)),
        policy.evaluate(customer()),
    ]
    report = summarise(decisions)
    assert report["stopped"] == 1
    assert report["by_reason"]["OPTOUT_RECEIVED"] == 1
