"""Gate decision maths and policy tightening, offline. The same properties are asserted
end-to-end against the database in tests/test_invariants.py.
"""

from __future__ import annotations

import itertools

import pytest

from drydock.gate import Facts, Tier, decide, kind_weight, next_min_confidence, risk

T0 = Tier(0, "Observe", 0, 0, False, 0)
T2 = Tier(2, "Merge (governed)", 30000, 200, True, 1.0)
T3 = Tier(3, "Merge (broad)", 60000, 2000, True, 5.0)


def f(added=0, changed=0, deleted=0, rows=36600, review=0, blocks=()):
    return Facts(added, changed, deleted, rows, review, tuple(blocks))


def test_kind_weight_is_max_over_kinds():
    assert kind_weight(f(added=5)) == 0.2
    assert kind_weight(f(added=5, changed=1)) == 0.7
    assert kind_weight(f(changed=5, deleted=1)) == 1.0


def test_absent_confidence_scores_at_full_weight():
    assert risk(f(changed=100), None) == pytest.approx(70.0)
    assert risk(f(changed=100), 0.0) == pytest.approx(70.0)
    assert risk(f(changed=100), 1.0) == 0.0


def test_high_confidence_small_change_merges():
    assert decide(f(changed=20000), 0.995, T2).decision == "MERGE"


def test_low_confidence_goes_pending_on_risk():
    d = decide(f(changed=850), 0.6, T2)
    assert d.decision == "PENDING" and d.reason.startswith("RISK")


def test_max_changed_is_a_ceiling_confidence_cannot_buy():
    d = decide(f(changed=30001), 1.0, T2)
    assert d.decision == "PENDING" and "TOO_MANY_ROWS" in d.reason


def test_delete_pct_over_tier_is_pending_not_blocked():
    # 600 dedup deletes = 1.64% of 36,600 > tier 2's 1%, so a reviewer decides
    d = decide(f(changed=600, deleted=600), 0.95, T2)
    assert d.decision == "PENDING" and d.reason.startswith("DELETE_PCT")


def test_18_6_hard_delete_ceiling_is_unbuyable_at_every_tier():
    # 40% of the table deleted, tier 3, confidence 1.0 -> BLOCKED.
    for tier in (T0, T2, T3):
        d = decide(f(deleted=14640), 1.0, tier)
        assert d.decision == "BLOCKED" and "DELETE_PCT_EXCEEDED" in d.reason


def test_hard_blocks_beat_everything_including_gate_off():
    for code in ("BASE_DRIFT", "SCHEMA_CHANGED", "BRANCH_NOT_OPEN", "UNACKNOWLEDGED_ERRORS:1"):
        d = decide(f(changed=1, blocks=[code]), 1.0, T3, gate_enabled=False)
        assert d.decision == "BLOCKED" and code in d.reason


def test_gate_off_merges_what_the_gate_would_hold():
    held = decide(f(changed=850, review=40), 0.6, T2)
    assert held.decision == "PENDING"
    assert decide(f(changed=850, review=40), 0.6, T2, gate_enabled=False).decision == "MERGE"


def test_panel_split_rows_force_review():
    d = decide(f(changed=100, review=3), 0.999, T2)
    assert d.decision == "PENDING" and "ROWS_NEED_REVIEW" in d.reason


def test_below_min_confidence_is_pending():
    d = decide(f(changed=10), 0.70, T2, min_confidence=0.75)
    assert d.decision == "PENDING" and "BELOW_MIN_CONFIDENCE" in d.reason


@pytest.mark.parametrize("conf", [0.0, 0.5, 0.99, 1.0])
def test_18_17_tier_zero_never_merges(conf):
    assert decide(f(changed=1), conf, T0).decision != "MERGE"


def test_threshold_line_slides_with_confidence():
    lo = decide(f(changed=10), 0.3, T2).limits["threshold"]
    hi = decide(f(changed=10), 0.9, T2).limits["threshold"]
    assert lo < hi


def test_18_5_min_confidence_only_moves_up():
    seq = ["WRONG_MATCH"] * 5 + [None] * 5 + ["POLICY", "TOO_BROAD", "OTHER", "INSUFFICIENT_EVIDENCE"]
    cur, history = 0.6, [0.6]
    for code in seq:
        cur = next_min_confidence(cur, code)
        history.append(cur)
    assert all(b >= a for a, b in itertools.pairwise(history)), history
    assert max(history) <= 0.98


def test_tightening_caps_at_098():
    cur = 0.9
    for _ in range(10):
        cur = next_min_confidence(cur, "WRONG_MATCH")
    assert cur == 0.98
