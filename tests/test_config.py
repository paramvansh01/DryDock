"""The verification guard, exercised with a test key against fixture files.
No product code path accepts a key argument.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from drydock.config import NotVerified, require_answered, require_verified, verified_status
from drydock.verification import sign

KEY = "test-key-not-the-real-one"
ROOT = Path(__file__).resolve().parent.parent


def entry(cid, status):
    return sign({"id": cid, "question": "q", "status": status, "summary": "s", "sql": "SELECT 1",
                 "stdout": "out", "stderr": "", "ms": 1, "runner": "admin", "ts": "t"}, KEY)


def write(tmp_path, entries):
    p = tmp_path / "verified.json"
    p.write_text(json.dumps(entries))
    return p


def test_signed_pass_passes(tmp_path):
    p = write(tmp_path, [entry("V4", "PASS"), entry("V5", "PASS")])
    require_verified("V4", "V5", path=p, key=KEY)


@pytest.mark.parametrize("status", ["FAIL", "UNKNOWN"])
def test_non_pass_raises_with_id(tmp_path, status):
    p = write(tmp_path, [entry("V4", "PASS"), entry("V5", status)])
    with pytest.raises(NotVerified, match=f"V5={status}"):
        require_verified("V4", "V5", path=p, key=KEY)


def test_hand_flipped_fail_to_pass_is_detected(tmp_path):
    e = entry("V4", "FAIL")
    e["status"] = "PASS"                     # what a shortcut-taking agent would do
    p = write(tmp_path, [e])
    assert verified_status("V4", p, KEY) == "TAMPERED"
    with pytest.raises(NotVerified, match="altered outside scripts/verify.py"):
        require_verified("V4", path=p, key=KEY)


def test_unsigned_pass_is_not_trusted(tmp_path):
    p = write(tmp_path, [{"id": "V4", "status": "PASS"}])
    with pytest.raises(NotVerified):
        require_verified("V4", path=p, key=KEY)


def test_edited_evidence_is_detected(tmp_path):
    e = entry("V5", "PASS")
    e["stdout"] = "fabricated output"
    p = write(tmp_path, [e])
    assert verified_status("V5", p, KEY) == "TAMPERED"


def test_wrong_key_is_detected(tmp_path):
    p = write(tmp_path, [entry("V4", "PASS")])
    assert verified_status("V4", p, "some-other-key-123") == "TAMPERED"


def test_missing_key_refuses(tmp_path, monkeypatch):
    monkeypatch.delenv("DRYDOCK_VERIFY_KEY", raising=False)
    p = write(tmp_path, [entry("V4", "PASS")])
    with pytest.raises(NotVerified, match="not set"):
        require_verified("V4", path=p)


def test_missing_check_is_unknown(tmp_path):
    p = write(tmp_path, [])
    with pytest.raises(NotVerified, match="V9=UNKNOWN"):
        require_verified("V9", path=p, key=KEY)


def test_require_answered_accepts_signed_fail(tmp_path):
    p = write(tmp_path, [entry("V4", "FAIL")])
    assert require_answered("V4", path=p, key=KEY) == {"V4": "FAIL"}


def test_only_verify_py_writes_verified_json():
    """No product code, agent code or other script may write verified.json."""
    offenders = []
    for p in list(ROOT.glob("drydock/*.py")) + list(ROOT.glob("agent/*.py")) + list(ROOT.glob("bench/*.py")) \
            + list(ROOT.glob("scripts/*.py")):
        if p.name == "verify.py":
            continue
        src = p.read_text()
        if re.search(r"VERIFIED_JSON\s*\.\s*write|verified\.json['\"]\s*\)\s*\.\s*write|open\([^)]*verified\.json[^)]*['\"]w",
                     src):
            offenders.append(str(p.relative_to(ROOT)))
    assert not offenders, offenders


def test_no_product_code_passes_a_key_to_the_guard():
    """The key= parameter exists for these tests only; product code must read it from the environment."""
    offenders = [str(p.relative_to(ROOT)) for p in list(ROOT.glob("drydock/*.py")) + list(ROOT.glob("agent/*.py"))
                 + list(ROOT.glob("bench/*.py"))
                 if p.name not in ("config.py", "verification.py")
                 and re.search(r"require_(verified|answered)\([^)]*key\s*=", p.read_text())]
    assert not offenders, offenders


def test_ladder_status_is_signed_like_verification(tmp_path):
    p = tmp_path / "ladder.json"
    e = entry("B6", "FAIL")
    e["status"] = "PASS"
    p.write_text(json.dumps({"meta": {"started_at": "t"}, "entries": [e]}))   # ladder.py's real format
    assert verified_status("B6", p, KEY) == "TAMPERED"
    p.write_text(json.dumps({"meta": {}, "entries": [entry("B6", "PASS")]}))
    assert verified_status("B6", p, KEY) == "PASS"


def test_in_db_adjudicator_requires_signed_ladder_evidence():
    import inspect
    from drydock import adjudicate
    src = inspect.getsource(adjudicate._exasol_ai)
    assert 'require_verified("V1", "V12", "B6")' in src


def test_published_signing_key_is_refused_everywhere(tmp_path, monkeypatch):
    """A key that appears in documentation is public, so it must sign nothing."""
    from drydock import config, verification
    placeholder = "make-up-any-secret-12-plus-chars"  # the published example value, not anyone's secret
    assert verification.is_published(placeholder)
    assert verification.key_problem(placeholder) is not None
    assert verification.key_problem("a-private-secret-nobody-typed") is None
    monkeypatch.setenv(verification.KEY_ENV, placeholder)
    with pytest.raises(config.NotVerified):
        config.require_verified("V5", path=tmp_path / "verified.json")
