"""Tamper-evident verification results.

verified.json is written only by scripts/verify.py. Each entry is signed with
HMAC-SHA256 under DRYDOCK_VERIFY_KEY, a secret supplied at run time and never stored in
the repository or in .env. require_verified() recomputes every signature, so an entry
edited by hand fails verification.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

KEY_ENV = "DRYDOCK_VERIFY_KEY"
SIGNED_FIELDS = ("id", "question", "status", "summary", "runner", "ts", "evidence_sha256")


class SignatureError(RuntimeError):
    pass


def evidence_digest(entry: dict) -> str:
    blob = json.dumps({k: entry.get(k, "") for k in ("sql", "stdout", "stderr")}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _payload(entry: dict) -> bytes:
    return json.dumps({k: entry.get(k) for k in SIGNED_FIELDS}, sort_keys=True, separators=(",", ":")).encode()


def sign(entry: dict, key: str) -> dict:
    if not key:
        raise SignatureError(f"{KEY_ENV} is empty")
    out = dict(entry)
    out["evidence_sha256"] = evidence_digest(out)
    out["sig"] = hmac.new(key.encode(), _payload(out), hashlib.sha256).hexdigest()
    return out


def check(entry: dict, key: str) -> None:
    """Raise SignatureError unless the entry's evidence and signature are intact."""
    if "sig" not in entry:
        raise SignatureError(f"{entry.get('id')}: unsigned (not written by scripts/verify.py)")
    if entry.get("evidence_sha256") != evidence_digest(entry):
        raise SignatureError(f"{entry.get('id')}: evidence (sql/stdout/stderr) altered after signing")
    want = hmac.new(key.encode(), _payload(entry), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, entry["sig"]):
        raise SignatureError(f"{entry.get('id')}: signature invalid (edited outside scripts/verify.py, or wrong key)")


# SHA-256 of example key values that appear in documentation. Anyone can read them, so they must never
# sign anything. Stored as hashes so the values are not repeated here.
PUBLISHED_KEY_SHA256 = frozenset({
    "8818da5ec2ba385e2917236563e0541ec81f63f72b59cff05452a4289f967398",
    "076ecbfb9858f988d82a87e2bcc6288e2e205ab624ada0ffb6c2d51038e3feaf",
})


def is_published(key: str) -> bool:
    return hashlib.sha256(key.encode()).hexdigest() in PUBLISHED_KEY_SHA256


def key_problem(key: str) -> str | None:
    """Why this signing key must not be used, or None. verify.py and ladder.py refuse on any answer."""
    if len(key) < 12:
        return "signing key must be at least 12 characters"
    if is_published(key):
        return ("this signing key is example text from the documentation, so its signatures prove nothing. "
                "Choose a secret of your own.")
    return None


def key_from_env() -> str | None:
    return os.environ.get(KEY_ENV) or None


def key_in_file(path: Path) -> bool:
    try:
        return any(line.strip().startswith(f"{KEY_ENV}=") and line.split("=", 1)[1].strip()
                   for line in path.read_text().splitlines())
    except FileNotFoundError:
        return False
