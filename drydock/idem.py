"""Idempotency for agent tool calls.

idem_key defaults to sha256(run_id | tool | canonical_json(args)). DRYDOCK.IDEMPOTENCY
has a primary key on IDEM_KEY, which Exasol enforces, so a concurrent duplicate is
rejected by the database.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable

from .db import Db, DbError, lit


def key_for(run_id: str | None, tool: str, args: dict) -> str:
    canon = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{run_id}|{tool}|{canon}".encode()).hexdigest()


def once(db: Db, idem_key: str, tool: str, fn: Callable[[], dict]) -> dict:
    """Run fn() at most once per idem_key. A replay returns the stored result
    with replayed=True and does nothing else."""
    prior = db.scalar(f"SELECT RESULT_JSON FROM DRYDOCK.IDEMPOTENCY WHERE IDEM_KEY = {lit(idem_key)}")
    if prior is not None:
        out: dict[str, Any] = json.loads(prior)
        out["replayed"] = True
        return out
    result = fn()
    try:
        db.run("INSERT INTO DRYDOCK.IDEMPOTENCY (IDEM_KEY, TOOL, RESULT_JSON) VALUES ("
               f"{lit(idem_key)}, {lit(tool)}, {lit(json.dumps(result, default=str)[:20000])})")
    except DbError:
        # PK collision: someone else won the race; theirs is the canonical result.
        prior = db.scalar(f"SELECT RESULT_JSON FROM DRYDOCK.IDEMPOTENCY WHERE IDEM_KEY = {lit(idem_key)}")
        if prior is not None:
            out = json.loads(prior)
            out["replayed"] = True
            return out
        raise
    result["replayed"] = False
    return result
