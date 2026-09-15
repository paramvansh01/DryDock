"""Risk-weighted merge gate, hard blocks and policy tightening.

decide() is pure and unit-tested. request_merge() gathers the observed facts from
the database, calls decide(), records the decision and, only on MERGE, hands off to
merge.apply(), the single write path to GOLDEN.

Decision order:
  hard block (every tier)                            -> BLOCKED
  gate disabled (A/B control only)                   -> MERGE
  tier cannot merge (MAX_CHANGED = 0)                -> PENDING
  total_changed > MAX_CHANGED
  deletes not allowed / delete_pct > MAX_DELETE_PCT
  confidence < run MIN_CONFIDENCE (tightened by rejections)
  risk > MAX_RISK
  rows the panel could not agree on                  -> PENDING
  else                                               -> MERGE
Above the tier's delete percentage a merge waits for a reviewer; above the global
HARD_DELETE_PCT it is blocked at every tier.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import events, idem
from .config import GOLDEN, SETTINGS, TIGHTENING_CODES
from .db import Db, ident, lit


@dataclass(frozen=True)
class Tier:
    tier: int
    label: str
    max_changed: int
    max_risk: float
    allow_deletes: bool
    max_delete_pct: float


@dataclass(frozen=True)
class Facts:
    added: int
    changed: int
    deleted: int
    table_rows: int                # GOLDEN rows at copy time (denominator for delete %)
    needs_review: int              # rows with APPROVED = FALSE by default (panel SPLIT)
    hard_blocks: tuple[str, ...]   # codes from hard_blocks()

    @property
    def total(self) -> int:
        return self.added + self.changed + self.deleted

    @property
    def delete_pct(self) -> float:
        return 100.0 * self.deleted / self.table_rows if self.table_rows else 0.0


@dataclass(frozen=True)
class Decision:
    decision: str                  # MERGE | PENDING | BLOCKED
    reason: str
    risk: float
    limits: dict


def kind_weight(f: Facts) -> float:
    if f.deleted > 0:
        return 1.0
    if f.changed > 0:
        return 0.7
    if f.added > 0:
        return 0.2
    return 0.0


def risk(f: Facts, confidence: float | None) -> float:
    c = 0.0 if confidence is None else max(0.0, min(1.0, float(confidence)))
    return f.total * (1.0 - c) * kind_weight(f)


def decide(f: Facts, confidence: float | None, tier: Tier, *, gate_enabled: bool = True,
           min_confidence: float = 0.0, hard_delete_pct: float | None = None) -> Decision:
    hard_delete_pct = SETTINGS.hard_delete_pct if hard_delete_pct is None else hard_delete_pct
    r = risk(f, confidence)
    conf = 0.0 if confidence is None else float(confidence)
    limits = {"max_changed": tier.max_changed, "max_risk": tier.max_risk, "max_delete_pct": tier.max_delete_pct,
              "hard_delete_pct": hard_delete_pct, "min_confidence": min_confidence,
              "threshold": tier.max_changed * conf, "delete_pct": round(f.delete_pct, 4),
              "total_changed": f.total, "confidence": conf}
    blocks = list(f.hard_blocks)
    if f.delete_pct > hard_delete_pct:
        blocks.append("DELETE_PCT_EXCEEDED")
    if blocks:
        return Decision("BLOCKED", ",".join(blocks), r, limits)
    if not gate_enabled:
        return Decision("MERGE", "GATE_DISABLED (A/B control)", r, limits)
    if tier.max_changed <= 0:
        return Decision("PENDING", f"TIER_{tier.tier}_CANNOT_MERGE", r, limits)
    if f.total > tier.max_changed:
        return Decision("PENDING", f"TOO_MANY_ROWS ({f.total} > {tier.max_changed})", r, limits)
    if f.deleted > 0 and not tier.allow_deletes:
        return Decision("PENDING", "DELETES_NOT_ALLOWED_AT_TIER", r, limits)
    if f.delete_pct > tier.max_delete_pct:
        return Decision("PENDING", f"DELETE_PCT ({f.delete_pct:.2f}% > {tier.max_delete_pct}%)", r, limits)
    if conf < min_confidence:
        return Decision("PENDING", f"BELOW_MIN_CONFIDENCE ({conf:.2f} < {min_confidence:.2f})", r, limits)
    if r > tier.max_risk:
        return Decision("PENDING", f"RISK ({r:.1f} > {tier.max_risk})", r, limits)
    if f.needs_review > 0:
        return Decision("PENDING", f"ROWS_NEED_REVIEW ({f.needs_review} panel-split rows)", r, limits)
    return Decision("MERGE", "WITHIN_TIER", r, limits)


def next_min_confidence(current: float, reject_code: str | None) -> float:
    """Tightening only: never decreases, capped at 0.98 (test 18.5)."""
    if reject_code in TIGHTENING_CODES:
        return max(current, min(0.98, current + 0.05))
    return current


# ------------------------------------------------------------------ DB-facing

def load_tier(db: Db, tier: int) -> Tier:
    r = db.dicts(f"SELECT * FROM DRYDOCK.TIER_CONFIG WHERE TIER = {int(tier)}")
    if not r:
        raise LookupError(f"no tier {tier}")
    t = r[0]
    return Tier(int(t["TIER"]), t["LABEL"], int(t["MAX_CHANGED"]), float(t["MAX_RISK"]),
                bool(t["ALLOW_DELETES"]), float(t["MAX_DELETE_PCT"]))


def run_settings(db: Db, run_id: str | None) -> tuple[int, bool, float]:
    if not run_id:
        return SETTINGS.default_tier, True, 0.0
    r = db.dicts(f"SELECT TIER, GATE_ENABLED, MIN_CONFIDENCE FROM DRYDOCK.RUNS WHERE RUN_ID = {lit(run_id)}")
    if not r:
        return SETTINGS.default_tier, True, 0.0
    return int(r[0]["TIER"]), bool(r[0]["GATE_ENABLED"]), float(r[0]["MIN_CONFIDENCE"] or 0)


def table_blocks(tables: list[dict]) -> list[str]:
    """One GOLDEN table per merge. The swap, the recorded fingerprints, the unmerge check and crash recovery
    are all per table, and are exercised on one; a change that touches two tables is refused, not half-handled.
    (GOLDEN holds one table today and branch SQL cannot create another, so this is a guarantee, not a limit.)"""
    return ["MULTI_TABLE"] if len({t["table"] for t in tables}) > 1 else []


def hard_blocks(db: Db, branch_id: str, diff: dict) -> list[str]:
    """Observed facts that no tier, confidence or reviewer can override."""
    from .hashing import columns, signature

    bid = ident(branch_id)
    out = table_blocks(diff.get("tables", []))
    br = db.dicts(f"SELECT STATUS FROM DRYDOCK.BRANCHES WHERE BRANCH_ID = {lit(bid)}")
    if not br or br[0]["STATUS"] != "OPEN":
        out.append("BRANCH_NOT_OPEN")
    if not diff.get("tables"):
        out.append("NO_DIFF")
    for t in diff.get("tables", []):
        if not t["table"].startswith(f"{GOLDEN}."):
            out.append(f"FORBIDDEN_TABLE:{t['table']}")
        if t.get("drifted_keys", 0) > 0:
            out.append("BASE_DRIFT")
        name = t["table"].split(".", 1)[1]
        mat = db.scalar(f"SELECT COLUMN_SIGNATURE FROM DRYDOCK.MATERIALISATIONS WHERE BRANCH_ID = {lit(bid)} "
                        f"AND SOURCE_TABLE = {lit(name)}")
        if signature(columns(db, bid, name)) != mat or signature(columns(db, GOLDEN, name)) != mat:
            out.append("SCHEMA_CHANGED")
    unack = int(db.scalar(f"SELECT COUNT(*) FROM DRYDOCK.BRANCH_OPS WHERE BRANCH_ID = {lit(bid)} "
                          "AND STATUS = 'ERROR' AND NOT ACKNOWLEDGED"))
    if unack:
        out.append(f"UNACKNOWLEDGED_ERRORS:{unack}")
    return sorted(set(out))


def facts_from(db: Db, branch_id: str, diff: dict, blocks: list[str]) -> Facts:
    bid = ident(branch_id)
    added = sum(t["added"] for t in diff["tables"])
    changed = sum(t["changed"] for t in diff["tables"])
    deleted = sum(t["deleted"] for t in diff["tables"])
    base_rows = int(db.scalar(f"SELECT COALESCE(SUM(BASE_ROWCOUNT), 0) FROM DRYDOCK.MATERIALISATIONS "
                              f"WHERE BRANCH_ID = {lit(bid)}") or 0)
    review = int(db.scalar(f"SELECT COUNT(*) FROM DRYDOCK.DIFF_ROWS WHERE BRANCH_ID = {lit(bid)} "
                           "AND NOT APPROVED AND DEFAULT_REASON <> 'HUMAN'") or 0)
    return Facts(added, changed, deleted, base_rows, review, tuple(blocks))


def request_merge(db: Db, branch_id: str, confidence: float | None, rationale: str = "",
                  idem_key: str | None = None, requested_by: str = "agent") -> dict:
    bid = ident(branch_id)
    run_id = db.scalar(f"SELECT RUN_ID FROM DRYDOCK.BRANCHES WHERE BRANCH_ID = {lit(bid)}")
    key = idem_key or idem.key_for(run_id, "request_merge", {"branch_id": bid})
    return idem.once(db, key, "request_merge",
                     lambda: _request(db, bid, run_id, confidence, rationale, key, requested_by))


def _request(db: Db, bid: str, run_id: str | None, confidence, rationale, key, requested_by) -> dict:
    from . import diff as diffmod
    from . import merge as mergemod

    d = diffmod.compute(db, bid, run_id)          # always re-observe at request time
    blocks = hard_blocks(db, bid, d)
    facts = facts_from(db, bid, d, blocks)
    tier_n, gate_enabled, min_conf = run_settings(db, run_id)
    br_gate = db.scalar(f"SELECT GATE_ENABLED FROM DRYDOCK.BRANCHES WHERE BRANCH_ID = {lit(bid)}")
    gate_enabled = gate_enabled and (br_gate is None or bool(br_gate))
    dec = decide(facts, confidence, load_tier(db, tier_n), gate_enabled=gate_enabled, min_confidence=min_conf)
    db.run("INSERT INTO DRYDOCK.MERGES (BRANCH_ID, REQUESTED_BY, IDEM_KEY, TOTAL_CHANGED, CONFIDENCE, RISK_SCORE, "
           f"TIER_AT_TIME, DECISION, BLOCK_REASON, RATIONALE) VALUES ({lit(bid)}, {lit(requested_by)}, {lit(key)}, "
           f"{facts.total}, {lit(confidence)}, {round(dec.risk, 4)}, {tier_n}, "
           f"{lit('APPLYING' if dec.decision == 'MERGE' else dec.decision)}, {lit(dec.reason[:512])}, "
           f"{lit(rationale[:4000])})")
    mid = int(db.scalar(f"SELECT MAX(MERGE_ID) FROM DRYDOCK.MERGES WHERE BRANCH_ID = {lit(bid)}"))
    events.emit("merge.requested", run_id, bid, merge_id=mid, confidence=float(confidence or 0),
                rationale=rationale, total_changed=facts.total)
    events.emit("merge.gated", run_id, bid, merge_id=mid, decision=dec.decision, reason=dec.reason,
                risk=round(dec.risk, 3), tier=tier_n, limits=dec.limits, rows_needing_review=facts.needs_review)
    if dec.decision == "BLOCKED":
        db.run(f"UPDATE DRYDOCK.BRANCHES SET STATUS = 'BLOCKED' WHERE BRANCH_ID = {lit(bid)}")
    out = {"merge_id": mid, "decision": dec.decision, "reason": dec.reason, "risk": round(dec.risk, 3),
           "rows_applied": 0, "block_reason": dec.reason if dec.decision == "BLOCKED" else None,
           "rows_needing_review": facts.needs_review}
    if dec.decision == "MERGE":
        applied = mergemod.apply(db, mid, resolved_by="gate")
        out.update(decision=applied["decision"], rows_applied=applied.get("rows_applied", 0),
                   block_reason=applied.get("block_reason"))
    return out


def list_pending(db: Db, include_resolved: bool = False, run_id: str | None = None) -> list[dict]:
    return db.dicts("SELECT m.MERGE_ID, m.BRANCH_ID, b.DEFECT_CLASS, m.DECISION, m.BLOCK_REASON, m.TOTAL_CHANGED, "
                    "m.CONFIDENCE, m.RISK_SCORE, m.ROWS_APPLIED, m.ROWS_EXCLUDED, m.REJECT_CODE, m.REJECT_NOTE, "
                    "m.RESOLVED_BY, m.UNMERGED_AT FROM DRYDOCK.MERGES m JOIN DRYDOCK.BRANCHES b "
                    f"ON b.BRANCH_ID = m.BRANCH_ID WHERE ({lit(include_resolved)} OR m.DECISION = 'PENDING') "
                    f"AND ({lit(run_id)} IS NULL OR b.RUN_ID = {lit(run_id)}) ORDER BY m.MERGE_ID")


def reject(db: Db, merge_id: int, code: str, note: str | None, by: str = "human") -> dict:
    """Human-only (REST). Records the rejection, tightens policy, discards the branch."""
    from . import branch as branchmod

    m = db.dicts(f"SELECT m.*, b.RUN_ID FROM DRYDOCK.MERGES m JOIN DRYDOCK.BRANCHES b ON b.BRANCH_ID = m.BRANCH_ID "
                 f"WHERE m.MERGE_ID = {int(merge_id)}")
    if not m or m[0]["DECISION"] not in ("PENDING", "BLOCKED"):
        raise ValueError(f"merge {merge_id} is not pending")
    m = m[0]
    db.run(f"UPDATE DRYDOCK.MERGES SET DECISION = 'REJECTED', REJECT_CODE = {lit(code)}, REJECT_NOTE = {lit(note)}, "
           f"RESOLVED_AT = CURRENT_TIMESTAMP, RESOLVED_BY = {lit(by)} WHERE MERGE_ID = {int(merge_id)}")
    events.emit("merge.rejected", m["RUN_ID"], m["BRANCH_ID"], merge_id=int(merge_id), code=code, note=note)
    adapt_policy(db, m["RUN_ID"], code)
    if db.scalar(f"SELECT STATUS FROM DRYDOCK.BRANCHES WHERE BRANCH_ID = {lit(m['BRANCH_ID'])}") == "BLOCKED":
        db.run(f"UPDATE DRYDOCK.BRANCHES SET STATUS = 'OPEN' WHERE BRANCH_ID = {lit(m['BRANCH_ID'])}")
    branchmod.discard_branch(db, m["BRANCH_ID"], f"merge {merge_id} rejected: {code}")
    return {"ok": True}


def adapt_policy(db: Db, run_id: str | None, code: str) -> float | None:
    if not run_id:
        return None
    cur = float(db.scalar(f"SELECT COALESCE(MIN_CONFIDENCE, 0) FROM DRYDOCK.RUNS WHERE RUN_ID = {lit(run_id)}") or 0)
    new = next_min_confidence(cur, code)
    # Server-side GREATEST: even a buggy caller cannot loosen the policy.
    db.run(f"UPDATE DRYDOCK.RUNS SET MIN_CONFIDENCE = GREATEST(COALESCE(MIN_CONFIDENCE, 0), {new}), "
           f"REJECTIONS_SEEN = COALESCE(REJECTIONS_SEEN, 0) + 1 WHERE RUN_ID = {lit(run_id)}")
    if new != cur:
        events.emit("policy.adapted", run_id, None, min_confidence_before=cur, min_confidence_after=new,
                    reason=f"merge rejected: {code}")
    return new
