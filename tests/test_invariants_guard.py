"""Guards on the invariant tests. Runs offline.

  - every locked assertion in tests/invariants.lock.json is still present, verbatim
    (adding is fine; removing or editing one fails until the lock is renewed with a reason)
  - no mocking of any kind inside the invariants file
  - no trivially true assertion shapes
  - every invariant has a live test
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from lock_invariants import extract  # noqa: E402

SRC = (ROOT / "tests" / "test_invariants.py").read_text()
LOCK = json.loads((ROOT / "tests" / "invariants.lock.json").read_text())


def test_no_locked_assertion_removed_or_changed():
    current = extract(SRC)
    missing = {t: sorted(set(a) - set(current.get(t, []))) for t, a in LOCK["tests"].items()}
    missing = {t: a for t, a in missing.items() if a}
    assert not missing, ("locked invariant assertions were removed or edited — this is the one thing you may not "
                         f"do to make a build pass:\n{json.dumps(missing, indent=2)}")


def test_no_mocking_in_invariants():
    for pat in (r"\bunittest\.mock\b", r"\bmock\b", r"\bMagicMock\b", r"\bmonkeypatch\b", r"\bpatch\(", r"\bmocker\b"):
        assert not re.search(pat, SRC), f"mocking construct {pat!r} in test_invariants.py"


def test_no_trivially_true_assertions():
    tree = ast.parse(SRC)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assert):
            continue
        src = ast.unparse(node.test)
        assert not (isinstance(node.test, ast.Constant) and node.test.value), f"assert constant: {src}"
        assert not re.search(r">=\s*0\b(?!\.)", src), f"'>= 0' is always true for counts: {src}"
        assert " or True" not in src and "is not None or" not in src, src


REQUIRED = {
    "18.1": "test_18_1_", "18.2": "test_18_2_", "18.3": "test_18_3_", "18.4": "test_18_4_privilege_floor",
    "18.5": "test_18_5_", "18.6": "test_18_6_", "18.7": "test_18_7_base_drift_on_a_touched_key_blocks",
    "18.8": "test_18_8_", "18.9": "test_18_9_", "18.10": "test_18_10_", "18.11": "test_18_11_",
    "18.12": "test_phase4_exit_and_18_12_", "18.13": "test_18_13_", "18.14": "test_18_14_",
    "18.15": "test_18_15_", "18.16": "test_18_16_", "18.17": "test_18_17_",
    "phase1-exit": "test_loaded_counts", "phase2-exit": "test_phase2_exit_",
}


def test_every_invariant_has_a_live_test():
    names = set(extract(SRC))
    missing = [k for k, prefix in REQUIRED.items() if not any(n.startswith(prefix) for n in names)]
    assert not missing, missing


def test_the_137_is_exact_not_approximate():
    assert "(d['changed'], d['added'], d['deleted']) == (137, 0, 0)" in LOCK["tests"]["test_18_3_diff_exactness_137"] \
        or "(d[\"changed\"], d[\"added\"], d[\"deleted\"]) == (137, 0, 0)" in SRC
