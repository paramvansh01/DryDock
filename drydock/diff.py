"""Row-level diff engine. All classification runs in Exasol.

Per materialised table:
  1. DIFF_ROWS: full outer join of the branch's row hashes against the BASE_HASHES
     snapshot taken at copy time -> ADDED / CHANGED / DELETED.
  2. COLS_MASK per CHANGED row: bit i is set when column i differs.
  3. Key-scoped drift: a touched key whose current GOLDEN hash no longer matches its
     snapshot is drifted, and merge refuses it.
  4. UI cards: before/after values, rows in rarely touched columns first.
Tables without a registered key get a multiset diff (ADDED/DELETED only).

The hash, mask and classification SQL is generated from the catalogue. The planner
only ever receives summary_for_agent(): counts and column names, never values.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from . import events
from .config import GOLDEN, SETTINGS, require_verified
from .db import Db, ident, lit, qname
from .hashing import Column, canon, columns, key_text, row_hash_expr, table_fingerprint
from .lintguard import clean

MAX_MASK_COLUMNS = 60

# Hooks registered by the ER layer (drydock/er.py): default approvals from the
# panel, and pair enrichment + ordering for the UI cards.
DefaultsHook = Callable[[Db, str, str, str | None], dict]
EnrichHook = Callable[[Db, str, str | None, list[dict]], list[dict]]
HOOKS: dict[str, Callable | None] = {"defaults": None, "enrich": None}


def _mat_rows(db: Db, bid: str) -> list[dict]:
    return db.dicts(f"SELECT * FROM DRYDOCK.MATERIALISATIONS WHERE BRANCH_ID = {lit(bid)} ORDER BY MAT_ID")


def _key_col(db: Db, table: str) -> str | None:
    return db.scalar(f"SELECT KEY_COLUMN FROM DRYDOCK.TABLE_KEYS WHERE SCHEMA_NAME = {lit(GOLDEN)} "
                     f"AND TABLE_NAME = {lit(ident(table))}")


def mask_expr(cols: list[Column], a: str, b: str) -> str:
    terms = [f"CASE WHEN {canon(c.name, c.type, a)} <> {canon(c.name, c.type, b)} THEN {2 ** i} ELSE 0 END"
             for i, c in enumerate(cols[:MAX_MASK_COLUMNS])]
    return " + ".join(terms) if terms else "0"


def decode_mask(mask: int | None, order: list[str]) -> list[str]:
    if mask is None:
        return []
    m = int(mask)
    return [c for i, c in enumerate(order) if m >> i & 1]


def rarity_order(rows: list[dict], order: list[str], counts: dict[str, int]) -> list[dict]:
    """Rows touching the rarest columns first; ADDED/DELETED after
    changed-rare rows; never uniform-random. Stable on key for determinism."""
    def k(r):
        cols = decode_mask(r.get("COLS_MASK"), order) if r["CLASS"] == "CHANGED" else []
        rare = min((counts.get(c, 0) for c in cols), default=10**12)
        cls = {"CHANGED": 0, "DELETED": 1, "ADDED": 2}[r["CLASS"]]
        return (rare, cls, str(r["KEY_VALUE"]))
    return sorted(rows, key=k)


def drift_count(db: Db, bid: str, table: str, cols: list[Column], key: str) -> int:
    """Key-scoped drift: touched keys whose GOLDEN row moved since the copy."""
    t = ident(table)
    g = (f"SELECT {key_text(cols, key)} AS K, {row_hash_expr(cols)} AS H FROM {qname(GOLDEN, t)}")
    return int(db.scalar(clean(
        f"SELECT COUNT(*) FROM DRYDOCK.DIFF_ROWS d LEFT JOIN ({g}) g ON g.K = d.KEY_VALUE "
        f"WHERE d.BRANCH_ID = {lit(bid)} AND d.TARGET_TABLE = {lit(t)} AND ("
        "(d.CLASS IN ('CHANGED', 'DELETED') AND (g.K IS NULL OR g.H <> d.BASE_HASH)) OR "
        "(d.CLASS = 'ADDED' AND g.K IS NOT NULL))", "diff.drift")))


KEY_ALIAS = "DRYDOCK_KEY"   # the key is selected twice (as key, and as a data column); without an alias the
                            # driver refuses the duplicate column name.


def _values_sql(schema: str, table: str, kt: str, names: list[str], chunk: list[str]) -> str:
    return (f"SELECT {kt} AS {KEY_ALIAS}, {', '.join(names)} FROM {qname(schema, table)} "
            f"WHERE {kt} IN ({', '.join(lit(k) for k in chunk)})")


def _fetch_values(db: Db, schema: str, table: str, key: str, cols: list[Column], keys: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    names = [c.name for c in cols]
    kt = key_text(cols, key)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        if not chunk:
            continue
        rows = db.rows(_values_sql(schema, table, kt, names, chunk))
        for r in rows:
            out[str(r[0])] = {n: (v.isoformat() if hasattr(v, "isoformat") else v) for n, v in zip(names, r[1:])}
    return out


def _reapply_human(db: Db, bid: str, t: str, human: list[tuple]) -> None:
    for flag in (True, False):
        keys = [str(k) for k, a in human if bool(a) is flag]
        for i in range(0, len(keys), 500):
            db.run(f"UPDATE DRYDOCK.DIFF_ROWS SET APPROVED = {lit(flag)}, DEFAULT_REASON = 'HUMAN' "
                   f"WHERE BRANCH_ID = {lit(bid)} AND TARGET_TABLE = {lit(t)} "
                   f"AND KEY_VALUE IN ({', '.join(lit(k) for k in keys[i:i + 500])})")


def compute(db: Db, branch_id: str, run_id: str | None = None, emit: bool = True) -> dict:
    """Full diff of every materialised table in the branch. Never mutates GOLDEN."""
    require_verified("V6")
    bid = ident(branch_id)
    br = db.dicts(f"SELECT RUN_ID, DEFECT_CLASS FROM DRYDOCK.BRANCHES WHERE BRANCH_ID = {lit(bid)}")
    run_id = run_id or (br[0]["RUN_ID"] if br else None)
    tables_out = []
    total_changed = 0
    any_drift = False
    t_all = time.perf_counter()
    for m in _mat_rows(db, bid):
        t = m["SOURCE_TABLE"]
        t0 = time.perf_counter()
        head_rows = int(db.scalar(f"SELECT COUNT(*) FROM {bid}.{ident(t)}"))
        if head_rows > SETTINGS.diff_ceiling:
            raise RuntimeError(f"DIFF_CEILING: {bid}.{t} has {head_rows} rows > {SETTINGS.diff_ceiling}")
        cols = columns(db, bid, t)
        order = [c.name for c in cols[:MAX_MASK_COLUMNS]]
        key = _key_col(db, t)
        mode = m["KEY_MODE"] or ("KEYED" if key else "KEYLESS")
        # Human row decisions survive a re-diff (request_merge and apply both re-observe).
        human = db.rows(f"SELECT KEY_VALUE, APPROVED FROM DRYDOCK.DIFF_ROWS WHERE BRANCH_ID = {lit(bid)} "
                        f"AND TARGET_TABLE = {lit(t)} AND DEFAULT_REASON = 'HUMAN'")
        db.run(f"DELETE FROM DRYDOCK.DIFF_ROWS WHERE BRANCH_ID = {lit(bid)} AND TARGET_TABLE = {lit(t)}")
        head_hash = f"SELECT {row_hash_expr(cols)} AS H FROM {bid}.{ident(t)}"
        base_sel = (f"SELECT KEY_VALUE AS K, H FROM DRYDOCK.BASE_HASHES WHERE BRANCH_ID = {lit(bid)} "
                    f"AND TARGET_TABLE = {lit(t)}")

        if mode == "KEYED":
            head_sel = f"SELECT {key_text(cols, key)} AS K, {row_hash_expr(cols)} AS H FROM {bid}.{ident(t)}"
            db.run(clean(
                "INSERT INTO DRYDOCK.DIFF_ROWS (BRANCH_ID, TARGET_SCHEMA, TARGET_TABLE, KEY_VALUE, CLASS, "
                "COLS_MASK, BASE_HASH, HEAD_HASH, APPROVED, DEFAULT_REASON) "
                f"SELECT {lit(bid)}, {lit(GOLDEN)}, {lit(t)}, COALESCE(h.K, b.K), "
                "CASE WHEN b.K IS NULL THEN 'ADDED' WHEN h.K IS NULL THEN 'DELETED' ELSE 'CHANGED' END, "
                f"{2 ** len(order) - 1}, b.H, h.H, TRUE, 'DEFAULT' "
                f"FROM ({base_sel}) b FULL OUTER JOIN ({head_sel}) h ON b.K = h.K "
                "WHERE b.K IS NULL OR h.K IS NULL OR b.H <> h.H", "diff.classify"))
            # Column mask for CHANGED rows, against GOLDEN's current values (valid
            # for every non-drifted key; drifted keys block the merge regardless).
            kg, kh = key_text(cols, key, "g"), key_text(cols, key, "h")
            db.run(clean(
                "MERGE INTO DRYDOCK.DIFF_ROWS d USING ("
                f"SELECT {kh} AS K, {mask_expr(cols, 'g', 'h')} AS M "
                f"FROM {qname(GOLDEN, t)} g JOIN {bid}.{ident(t)} h ON {kg} = {kh}"
                f") s ON (d.BRANCH_ID = {lit(bid)} AND d.TARGET_TABLE = {lit(t)} AND d.KEY_VALUE = s.K "
                "AND d.CLASS = 'CHANGED') WHEN MATCHED THEN UPDATE SET d.COLS_MASK = s.M", "diff.mask"))
            rows = db.dicts(f"SELECT KEY_VALUE, CLASS, COLS_MASK FROM DRYDOCK.DIFF_ROWS "
                            f"WHERE BRANCH_ID = {lit(bid)} AND TARGET_TABLE = {lit(t)}")
            added = sum(r["CLASS"] == "ADDED" for r in rows)
            deleted = sum(r["CLASS"] == "DELETED" for r in rows)
            changed = sum(r["CLASS"] == "CHANGED" for r in rows)
            counts: dict[str, int] = {c: 0 for c in order}
            for r in rows:
                if r["CLASS"] == "CHANGED":
                    for c in decode_mask(r["COLS_MASK"], order):
                        counts[c] += 1
            counts = {c: n for c, n in counts.items() if n}
            drifted = drift_count(db, bid, t, cols, key)
            defaults = {}
            if HOOKS["defaults"]:
                defaults = HOOKS["defaults"](db, bid, t, run_id) or {}
            _reapply_human(db, bid, t, human)
            ordered = rarity_order(rows, order, counts)
            pick = [str(r["KEY_VALUE"]) for r in ordered[:SETTINGS.ui_rows_cap]]
            before = _fetch_values(db, GOLDEN, t, key, cols, pick)
            after = _fetch_values(db, bid, t, key, cols, pick)
            appr = {str(r[0]): (bool(r[1]), r[2]) for r in db.rows(
                f"SELECT KEY_VALUE, APPROVED, DEFAULT_REASON FROM DRYDOCK.DIFF_ROWS WHERE BRANCH_ID = {lit(bid)} "
                f"AND TARGET_TABLE = {lit(t)}")}
            by_key = {str(r["KEY_VALUE"]): r for r in rows}
            cards = []
            for k in pick:
                r = by_key[k]
                ch = decode_mask(r["COLS_MASK"], order) if r["CLASS"] == "CHANGED" else []
                cards.append({"key": k, "class": r["CLASS"], "cols_changed": ch,
                              "before": before.get(k) if r["CLASS"] != "ADDED" else None,
                              "after": after.get(k) if r["CLASS"] != "DELETED" else None,
                              "approved": appr.get(k, (True, "DEFAULT"))[0],
                              "default_reason": appr.get(k, (True, "DEFAULT"))[1]})
            if HOOKS["enrich"]:
                cards = HOOKS["enrich"](db, bid, run_id, cards)
        else:
            agg = db.rows(clean(
                "SELECT COALESCE(SUM(CASE WHEN hc > bc THEN hc - bc ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN bc > hc THEN bc - hc ELSE 0 END), 0) FROM ("
                "SELECT H, SUM(CASE WHEN S = 'h' THEN 1 ELSE 0 END) AS hc, SUM(CASE WHEN S = 'b' THEN 1 ELSE 0 END) AS bc "
                f"FROM (SELECT H, 'h' AS S FROM ({head_hash}) UNION ALL SELECT H, 'b' AS S FROM ({base_sel})) "
                "GROUP BY H)", "diff.keyless"))[0]
            added, deleted, changed = int(agg[0]), int(agg[1]), 0
            counts, cards, drifted, defaults = {}, [], 0, {}
            cur = table_fingerprint(db, GOLDEN, t)
            drifted = 0 if cur.hex == m["BASE_FINGERPRINT"] else 1

        table_drift = table_fingerprint(db, GOLDEN, t).hex != m["BASE_FINGERPRINT"]
        any_drift = any_drift or drifted > 0
        unchanged = head_rows - added - changed
        ms = (time.perf_counter() - t0) * 1000
        total_changed += added + changed + deleted
        tinfo = {"table": f"{GOLDEN}.{t}", "mode": mode, "added": added, "changed": changed, "deleted": deleted,
                 "unchanged": unchanged, "columns_touched": counts, "column_order": order,
                 "rows": cards, "rows_total": added + changed + deleted, "base_drifted": bool(table_drift),
                 "drifted_keys": drifted, "diff_ms": round(ms, 1), "defaults": defaults}
        db.run("INSERT INTO DRYDOCK.DIFFS (BRANCH_ID, TARGET_SCHEMA, TARGET_TABLE, MODE, ROWS_ADDED, ROWS_CHANGED, "
               "ROWS_DELETED, ROWS_UNCHANGED, COLUMN_ORDER, COLUMNS_TOUCHED, SAMPLE, BASE_DRIFTED, DIFF_MS) VALUES ("
               f"{lit(bid)}, {lit(GOLDEN)}, {lit(t)}, {lit(mode)}, {added}, {changed}, {deleted}, {unchanged}, "
               f"{lit(json.dumps(order))}, {lit(json.dumps(counts))}, "
               f"{lit(json.dumps(cards[:SETTINGS.sample_rows], default=str)[:2000000])}, {lit(bool(table_drift))}, {int(ms)})")
        if emit:
            events.emit("diff.computed", run_id, bid, **{k: v for k, v in tinfo.items() if k != "drifted_keys"})
        tables_out.append(tinfo)
    return {"branch_id": bid, "tables": tables_out, "total_changed": total_changed,
            "base_drifted": any_drift, "diff_ms": round((time.perf_counter() - t_all) * 1000, 1),
            "mode": tables_out[0]["mode"] if tables_out else "EMPTY"}


def summary_for_agent(d: dict) -> dict:
    """What the planning model may see: counts, column names, drift. No row values."""
    return {
        "branch_id": d["branch_id"], "total_changed": d["total_changed"], "base_drifted": d["base_drifted"],
        "diff_ms": d["diff_ms"], "mode": d["mode"],
        "tables": [{k: t[k] for k in ("table", "mode", "added", "changed", "deleted", "unchanged",
                                      "columns_touched", "drifted_keys")}
                   | {"rows_needing_review": int((t.get("defaults") or {}).get("needs_review", 0))}
                   for t in d["tables"]],
    }


def set_approval(db: Db, branch_id: str, keys: list[str], approved: bool, run_id: str | None = None) -> dict:
    """Reviewer-only (REST): row-level approval toggle."""
    bid = ident(branch_id)
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        db.run(f"UPDATE DRYDOCK.DIFF_ROWS SET APPROVED = {lit(approved)}, DEFAULT_REASON = 'HUMAN' "
               f"WHERE BRANCH_ID = {lit(bid)} AND KEY_VALUE IN ({', '.join(lit(k) for k in chunk)})")
    n_ok, n = db.rows(f"SELECT SUM(CASE WHEN APPROVED THEN 1 ELSE 0 END), COUNT(*) FROM DRYDOCK.DIFF_ROWS "
                      f"WHERE BRANCH_ID = {lit(bid)}")[0]
    events.emit("rows.deselected", run_id, bid, keys=[str(k) for k in keys], approved=approved,
                approved_count=int(n_ok or 0), total=int(n or 0))
    return {"approved_count": int(n_ok or 0), "total": int(n or 0)}
