"""Antar - a causal revenue-recovery controller for Indian recurring payments.

Layering (PLAN.md section 4). Data flows strictly downward; no layer imports upward.

    signals  ->  detect  ->  decide  ->  act  ->  audit
      L1          L2         L3         L4      L5

`antar.detect` and `antar.decide` contain zero LLM calls. That claim is proven, not
asserted, by tests/unit/test_no_llm_in_detection.py and test_no_llm_in_decision.py.
"""

__version__ = "0.1.0"
