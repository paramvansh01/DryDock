"""Lock the assertions in tests/test_invariants.py so tests cannot quietly get easier.

    uv run python scripts/lock_invariants.py                      # show what would change
    uv run python scripts/lock_invariants.py --approve "<reason>"  # accept the current set

tests/test_invariants_guard.py fails if any locked assertion disappears or changes.
Adding assertions never needs a re-lock. Removing or editing one does, and --approve
records the reason in the lock file's history, so any change is visible in git.
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tests" / "test_invariants.py"
LOCK = ROOT / "tests" / "invariants.lock.json"


def extract(src: str) -> dict[str, list[str]]:
    tree = ast.parse(src)
    out: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            asserts = [ast.unparse(n) for n in ast.walk(node) if isinstance(n, ast.Assert)]
            raises = [ast.unparse(n) for n in ast.walk(node)
                      if isinstance(n, ast.With) and "pytest.raises" in ast.unparse(n.items[0])]
            out[node.name] = sorted(set(asserts + raises))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--approve", metavar="REASON")
    a = ap.parse_args()
    cur = extract(SRC.read_text())
    old = json.loads(LOCK.read_text())["tests"] if LOCK.exists() else {}
    removed = {t: sorted(set(v) - set(cur.get(t, []))) for t, v in old.items()}
    removed = {t: v for t, v in removed.items() if v}
    print(json.dumps({"removed_or_changed": removed}, indent=2))
    if not a.approve:
        return 0
    now = dt.datetime.now().isoformat(timespec="seconds")
    history = json.loads(LOCK.read_text()).get("history", []) if LOCK.exists() else []
    if old and removed:
        history.append({"at": now, "reason": a.approve, "removed_or_changed": removed})
    LOCK.write_text(json.dumps({"locked_at": now,
                                "sha256": hashlib.sha256(json.dumps(cur, sort_keys=True).encode()).hexdigest(),
                                "tests": cur, "history": history}, indent=2, sort_keys=True))
    print(f"locked {sum(len(v) for v in cur.values())} assertions across {len(cur)} tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
