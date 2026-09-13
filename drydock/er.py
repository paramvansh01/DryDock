"""Entity resolution, as system-owned steps the agent drives through two MCP tools
(stage_candidates, run_matching).

  mapping    the declared A->B column mapping; each B expression is validated
             against SOURCE_B.CLIENTS columns
  blocking   agent-written SELECT A_ID, B_ID over read-only schemas -> CANDIDATES_RAW
  scoring    one set-based pass in Exasol: per-signal scores
  panel      three SQL matchers vote: deterministic, probabilistic and skeptic
             (negative evidence: DOB conflict, Jr/Sr, business vs person)
  adjudicate SPLIT pairs only (adjudicate.py)
  closure    union-find over MERGE verdicts; components larger than 2 are flagged, never merged
  publish    ER_WORK.MATCHES_/NEW_/DEDUP_<run>: read-only decision tables used by branch SQL

All SQL here is generated and passes lintguard.clean() before it runs.
"""

from __future__ import annotations

import json
import re
import time
from collections import defaultdict

import sqlglot
from sqlglot import exp

from . import diff as diffmod
from . import events
from .config import ER_WORK, MATCH_CLASSES, require_verified
from .db import Db, ident, lit
from .lintguard import clean
from .retarget import Blocked, BranchState, retarget

GOLDEN_FIELDS = ("FULL_NAME", "EMAIL", "PHONE", "ADDR_LINE", "CITY", "POSTCODE", "COUNTRY", "DATE_OF_BIRTH")
B_COLUMNS = ("CLIENT_REF", "FIRST_NAME", "LAST_NAME", "EMAIL_ADDR", "MOBILE", "STREET", "TOWN", "ZIP",
             "NATION", "DOB", "LAST_SEEN")
_FN_ALLOW = {"TRIM", "LTRIM", "RTRIM", "UPPER", "LOWER", "TO_DATE", "CONCAT", "REPLACE", "REGEXP_REPLACE",
             "SUBSTR", "COALESCE", "NVL", "INITCAP", "CAST", "TO_CHAR", "LENGTH"}
SUFFIX_RX = r" (JR|SR|JNR|SNR|II|III)\.?$"
BUSINESS_RX = r" (LTD|LIMITED|PLC|INC|LLC|GMBH|BV|SL|CO)\.?$"
WEIGHTS = {"email": 30, "phone": 20, "name": 25, "addr": 15, "dob": 10}


class ErError(ValueError):
    pass


def run_tag(run_id: str) -> str:
    tag = re.sub(r"[^A-Z0-9_]", "_", run_id.upper())[:40]
    return ident(("R_" + tag) if not tag[0].isalpha() else tag)


# ------------------------------------------------------------------ mapping (pure)

def validate_b_expr(expr_sql: str) -> str:
    """A mapping fragment may reference SOURCE_B.CLIENTS columns (unqualified),
    literals, CASE, ||, and allowlisted scalar functions. Nothing else."""
    try:
        wrapper = sqlglot.parse_one(f"SELECT {expr_sql}", read="exasol")
    except Exception as ex:
        raise ErError(f"mapping expression does not parse: {expr_sql!r}: {ex}") from ex
    if not isinstance(wrapper, exp.Select) or len(wrapper.expressions) != 1 \
            or any(wrapper.find(t) for t in (exp.From, exp.Table, exp.Where, exp.Group, exp.Having, exp.Join,
                                             exp.Limit, exp.Order, exp.Subquery)):
        raise ErError(f"mapping must be exactly one scalar expression: {expr_sql!r}")
    e = wrapper.expressions[0]
    for node in e.walk():
        if isinstance(node, (exp.Select, exp.Subquery, exp.Table, exp.Query)):
            raise ErError(f"mapping expression may not contain a query: {expr_sql!r}")
        if isinstance(node, exp.Column):
            if node.table or node.name.upper() not in B_COLUMNS:
                raise ErError(f"mapping expression may only reference SOURCE_B.CLIENTS columns, got {node.sql()}")
        # Built-ins sqlglot recognises are scalar and side-effect free. Anything it does
        # not recognise (Anonymous) could be a UDF: allowlist only. Dotted calls
        # (schema.udf(...)) are refused outright.
        if isinstance(node, exp.Anonymous) and node.name.upper() not in _FN_ALLOW:
            raise ErError(f"function {node.name} not allowed in a mapping expression")
        if isinstance(node, exp.Dot):
            raise ErError(f"qualified function calls are not allowed: {node.sql()}")
    return expr_sql


def validate_mapping(mapping: dict) -> dict:
    missing = [f for f in GOLDEN_FIELDS if f not in mapping]
    if missing:
        raise ErError(f"mapping must define every golden field; missing {missing}")
    extra = set(mapping) - set(GOLDEN_FIELDS) - {"LAST_SEEN"}
    if extra:
        raise ErError(f"unknown golden fields {sorted(extra)}")
    return {k: validate_b_expr(str(v)) for k, v in mapping.items()}


def declare_mapping(db: Db, run_id: str, mapping: dict, rationale: str = "") -> dict:
    m = validate_mapping(mapping)
    db.run(f"DELETE FROM DRYDOCK.MAPPINGS WHERE RUN_ID = {lit(run_id)}")
    db.run(f"INSERT INTO DRYDOCK.MAPPINGS (RUN_ID, MAPPING) VALUES ({lit(run_id)}, {lit(json.dumps(m))})")
    events.emit("mapping.declared", run_id, None, mapping=m, rationale=rationale)
    return m


def load_mapping(db: Db, run_id: str) -> dict:
    raw = db.scalar(f"SELECT MAPPING FROM DRYDOCK.MAPPINGS WHERE RUN_ID = {lit(run_id)}")
    if raw is None:
        raise ErError("no mapping declared for this run — declare the A->B mapping first")
    return json.loads(raw)


# ------------------------------------------------------------------ blocking

def check_blocking_sql(select_sql: str) -> str:
    """The agent's blocking SELECT: read-only schemas only, output columns named A_ID and B_ID."""
    try:
        r = retarget(select_sql, BranchState("BR_BLOCKING"))
    except Blocked as b:
        raise ErError(f"blocking SQL refused: {b.code}: {b.message}") from b
    if r.kind not in ("SELECT", "UNION", "INTERSECT", "EXCEPT") or r.tables_written:
        raise ErError("blocking SQL must be a SELECT")
    if any(t.startswith("GOLDEN") for t in r.tables_read):
        raise ErError("blocking reads SOURCE_A / SOURCE_B / ER_WORK only")
    q = sqlglot.parse_one(select_sql, read="exasol")
    names = [n.upper() for n in q.named_selects]
    if names != ["A_ID", "B_ID"]:
        raise ErError(f"blocking SELECT must return exactly two columns named A_ID, B_ID (got {names})")
    return r.sql


def stage_candidates(db: Db, run_id: str, rule: str, select_sql: str, kind: str = "AB") -> dict:
    require_verified("V6")
    if kind not in ("AB", "AA"):
        raise ErError("kind is AB (SOURCE_A x SOURCE_B) or AA (duplicates inside SOURCE_A)")
    rule = re.sub(r"[^A-Z0-9_]", "_", rule.upper())[:60]
    sql = check_blocking_sql(select_sql)
    db.run(f"DELETE FROM DRYDOCK.CANDIDATES_RAW WHERE RUN_ID = {lit(run_id)} AND BLOCK_RULE = {lit(rule)} "
           f"AND KIND = {lit(kind)}")
    n, ms = db.timed(clean(
        "INSERT INTO DRYDOCK.CANDIDATES_RAW (RUN_ID, KIND, A_ID, B_ID, BLOCK_RULE) "
        f"SELECT DISTINCT {lit(run_id)}, {lit(kind)}, x.A_ID, x.B_ID, {lit(rule)} FROM ({sql}) x "
        "WHERE x.A_ID IS NOT NULL AND x.B_ID IS NOT NULL" + (" AND x.A_ID <> x.B_ID" if kind == "AA" else ""),
        "er.stage"))
    return {"rule": rule, "kind": kind, "pairs": n, "ms": round(ms, 1)}


# ------------------------------------------------------------------ normalised inputs

def regexp_like(expr: str, pattern: str) -> str:
    """Exasol's REGEXP_LIKE form is proven on the live instance (check L2), not assumed."""
    from .lintguard import dialect_lint
    proven = {e.title for e in dialect_lint.load_dialect() if e.status == "PROVEN"}
    if "REGEXP_LIKE infix predicate" in proven or "REGEXP_LIKE function form" not in proven:
        return f"{expr} REGEXP_LIKE '{pattern}'"
    return f"REGEXP_LIKE({expr}, '{pattern}')"


def _norm_select(src: str, e: dict[str, str], id_expr: str) -> str:
    """Standardised comparison columns. e maps golden field -> expression over the source."""
    name = f"UPPER(TRIM(REGEXP_REPLACE({e['FULL_NAME']}, '[^A-Za-z '']', '')))"
    base = f"REGEXP_REPLACE(REGEXP_REPLACE({name}, '{BUSINESS_RX}', ''), '{SUFFIX_RX}', '')"
    return (
        f"SELECT {id_expr} AS ID, {name} AS NAME_N, {base} AS NAME_BASE, "
        f"REGEXP_SUBSTR({base}, '^[^ ]+') AS GIVEN_N, REGEXP_SUBSTR({base}, '[^ ]+$') AS FAMILY_N, "
        f"REGEXP_SUBSTR({name}, '{SUFFIX_RX}') AS SUFFIX, "
        f"CASE WHEN {regexp_like(name, '.*' + BUSINESS_RX)} THEN 1 ELSE 0 END AS IS_BUSINESS, "
        f"LOWER(TRIM({e['EMAIL']})) AS EMAIL_N, "
        f"REGEXP_SUBSTR(LOWER(TRIM({e['EMAIL']})), '^[^@]+') AS EMAIL_LOCAL, "
        f"REGEXP_SUBSTR(LOWER(TRIM({e['EMAIL']})), '[^@]+$') AS EMAIL_DOMAIN, "
        f"RIGHT(REGEXP_REPLACE({e['PHONE']}, '[^0-9]', ''), 10) AS PHONE_N, "
        f"RIGHT(REGEXP_REPLACE({e['PHONE']}, '[^0-9]', ''), 7) AS PHONE7, "
        f"UPPER(REPLACE({e['POSTCODE']}, ' ', '')) AS POSTCODE_N, "
        f"UPPER(REGEXP_REPLACE({e['ADDR_LINE']}, '[^A-Za-z0-9]', '')) AS STREET_N, "
        f"UPPER(TRIM({e['CITY']})) AS CITY_N, {e['DATE_OF_BIRTH']} AS DOB "
        f"FROM {src}")


def build_norm(db: Db, run_id: str) -> tuple[str, str]:
    tag = run_tag(run_id)
    m = load_mapping(db, run_id)
    a_expr = {f: f for f in GOLDEN_FIELDS}
    a_tab, b_tab = f"{ER_WORK}.A_NORM_{tag}", f"{ER_WORK}.B_NORM_{tag}"
    db.run(clean(f"CREATE OR REPLACE TABLE {a_tab} AS {_norm_select('SOURCE_A.CUSTOMERS', a_expr, 'CUST_ID')}",
                 "er.norm_a"))
    db.run(clean(f"CREATE OR REPLACE TABLE {b_tab} AS {_norm_select('SOURCE_B.CLIENTS', m, 'CLIENT_REF')}",
                 "er.norm_b"))
    return a_tab, b_tab


# ------------------------------------------------------------------ scoring + panel (SQL)

def signals_select(a: str, b: str) -> str:
    """Per-signal scores as per-mille integers (valid JSON without TO_CHAR float formatting)."""
    s_email = ("CASE WHEN a.EMAIL_N IS NULL OR b.EMAIL_N IS NULL THEN NULL WHEN a.EMAIL_N = b.EMAIL_N THEN 1000 "
               "WHEN a.EMAIL_LOCAL = b.EMAIL_LOCAL THEN 600 WHEN a.EMAIL_DOMAIN = b.EMAIL_DOMAIN THEN 200 ELSE 0 END")
    s_phone = ("CASE WHEN a.PHONE_N IS NULL OR b.PHONE_N IS NULL THEN NULL WHEN a.PHONE_N = b.PHONE_N THEN 1000 "
               "WHEN a.PHONE7 = b.PHONE7 THEN 700 ELSE 0 END")
    s_name = ("CASE WHEN a.NAME_BASE IS NULL OR b.NAME_BASE IS NULL THEN NULL ELSE GREATEST("
              "FLOOR(1000 - 1000 * EDIT_DISTANCE(a.NAME_BASE, b.NAME_BASE) / GREATEST(LENGTH(a.NAME_BASE), LENGTH(b.NAME_BASE))), "
              "CASE WHEN a.FAMILY_N = b.FAMILY_N AND SUBSTR(a.GIVEN_N, 1, 1) = SUBSTR(b.GIVEN_N, 1, 1) THEN 850 ELSE 0 END) END")
    s_addr = ("(CASE WHEN a.POSTCODE_N = b.POSTCODE_N THEN 500 ELSE 0 END + "
              "CASE WHEN a.STREET_N = b.STREET_N THEN 500 WHEN SUBSTR(a.STREET_N, 1, 6) = SUBSTR(b.STREET_N, 1, 6) "
              "THEN 300 ELSE 0 END)")
    s_dob = "CASE WHEN a.DOB IS NULL OR b.DOB IS NULL THEN NULL WHEN a.DOB = b.DOB THEN 1000 ELSE 0 END"
    dob_gap = "CASE WHEN a.DOB IS NULL OR b.DOB IS NULL THEN NULL ELSE ABS(YEARS_BETWEEN(a.DOB, b.DOB)) END"
    return (f"SELECT c.PAIR_ID, {s_email} AS S_EMAIL, {s_phone} AS S_PHONE, {s_name} AS S_NAME, "
            f"{s_addr} AS S_ADDR, {s_dob} AS S_DOB, {dob_gap} AS DOB_GAP, "
            "CASE WHEN COALESCE(a.SUFFIX, '-') <> COALESCE(b.SUFFIX, '-') THEN 1 ELSE 0 END AS F_SUFFIX, "
            "CASE WHEN a.IS_BUSINESS <> b.IS_BUSINESS THEN 1 ELSE 0 END AS F_BUSINESS, "
            "CASE WHEN SUBSTR(a.GIVEN_N, 1, 1) <> SUBSTR(b.GIVEN_N, 1, 1) THEN 1 ELSE 0 END AS F_GIVEN, "
            "CASE WHEN a.FAMILY_N = b.FAMILY_N THEN 1 ELSE 0 END AS SAME_FAMILY "
            f"FROM DRYDOCK.CANDIDATES c JOIN {a} a ON a.ID = c.A_ID JOIN {b} b ON b.ID = c.B_ID")


def panel_sql(run_id: str, kind: str, a: str, b: str, prob_threshold: float) -> str:
    w = WEIGHTS
    num = " + ".join(f"COALESCE({w[k]} * S_{k.upper()}, 0)" for k in w)
    den = " + ".join(f"CASE WHEN S_{k.upper()} IS NULL THEN 0 ELSE {w[k]} END" for k in w)
    score = f"CASE WHEN ({den}) = 0 THEN 0 ELSE ({num}) / (1000.0 * ({den})) END"
    determ = "CASE WHEN S_EMAIL = 1000 OR S_PHONE = 1000 THEN 'MERGE' ELSE 'REJECT' END"
    prob = f"CASE WHEN {score} >= {prob_threshold:.4f} THEN 'MERGE' ELSE 'REJECT' END"
    skeptic = ("CASE WHEN (S_DOB = 0) OR F_SUFFIX = 1 OR F_BUSINESS = 1 OR F_GIVEN = 1 THEN 'REJECT' "
               "WHEN S_NAME >= 800 AND (S_DOB = 1000 OR S_EMAIL = 1000 OR S_PHONE >= 700 OR S_ADDR >= 800) "
               "THEN 'MERGE' ELSE 'REJECT' END")
    j = lambda k: f"COALESCE(TO_CHAR({k}), 'null')"  # noqa: E731
    sig_json = ("'{\"email\":' || " + j("S_EMAIL") + " || ',\"phone\":' || " + j("S_PHONE") +
                " || ',\"name\":' || " + j("S_NAME") + " || ',\"addr\":' || " + j("S_ADDR") +
                " || ',\"dob\":' || " + j("S_DOB") + " || ',\"dob_gap_years\":' || " + j("DOB_GAP") +
                " || ',\"suffix_conflict\":' || TO_CHAR(F_SUFFIX) || ',\"business_conflict\":' || TO_CHAR(F_BUSINESS)"
                " || ',\"given_initial_conflict\":' || TO_CHAR(F_GIVEN) || ',\"same_family\":' || TO_CHAR(SAME_FAMILY) || '}'")
    inner = signals_select(a, b) + f" WHERE c.RUN_ID = {lit(run_id)} AND c.KIND = {lit(kind)}"
    return (
        "MERGE INTO DRYDOCK.CANDIDATES t USING ("
        f"SELECT PAIR_ID, {sig_json} AS SIGNALS, ROUND({score}, 4) AS SCORE_TOTAL, "
        f"{determ} AS VD, {prob} AS VP, {skeptic} AS VS FROM ({inner})"
        ") s ON (t.PAIR_ID = s.PAIR_ID) WHEN MATCHED THEN UPDATE SET t.SIGNALS = s.SIGNALS, "
        "t.SCORE_TOTAL = s.SCORE_TOTAL, t.VOTE_DETERM = s.VD, t.VOTE_PROB = s.VP, t.VOTE_SKEPTIC = s.VS, "
        "t.PANEL_RESULT = CASE WHEN s.VD = 'MERGE' AND s.VP = 'MERGE' AND s.VS = 'MERGE' THEN 'UNANIMOUS_MERGE' "
        "WHEN s.VD = 'REJECT' AND s.VP = 'REJECT' AND s.VS = 'REJECT' THEN 'UNANIMOUS_REJECT' ELSE 'SPLIT' END")


# ------------------------------------------------------------------ closure (pure)

class UnionFind:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)   # deterministic root


def components(pairs: list[tuple[int, str, str, str]]) -> dict[int, tuple[str, int]]:
    """pairs: (pair_id, kind, a_id, b_id) with VERDICT MERGE. Returns pair_id -> (component_id, size).
    Node names are namespaced: an A id and a B id are different records even if equal strings."""
    uf = UnionFind()
    nodes = {}
    for pid, kind, a, b in pairs:
        na, nb = f"A:{a}", (f"A:{b}" if kind == "AA" else f"B:{b}")
        uf.union(na, nb)
        nodes[pid] = na
    members: dict[str, set[str]] = defaultdict(set)
    for pid, kind, a, b in pairs:
        root = uf.find(f"A:{a}")
        members[root] |= {f"A:{a}", f"A:{b}" if kind == "AA" else f"B:{b}"}
    return {pid: (uf.find(na), len(members[uf.find(na)])) for pid, na in nodes.items()}


def compare_summary(sig: dict) -> str:
    """Non-identifying comparison text: what the adjudicator (and precedent retrieval) sees.
    No names, emails, phones, addresses or dates — only how two records compare."""
    def lvl(v, hi="equal", mid="similar", lo="different"):
        if v is None:
            return "missing on one side"
        return hi if v >= 950 else mid if v >= 600 else lo
    parts = [f"full name {lvl(sig.get('name'))} ({(sig.get('name') or 0) / 1000:.2f})",
             "same surname" if sig.get("same_family") else "different surname",
             "given-name initial differs" if sig.get("given_initial_conflict") else "given-name initial matches",
             "generational suffix conflict (e.g. Sr vs Jr)" if sig.get("suffix_conflict") else "no suffix conflict",
             "one side is a business name" if sig.get("business_conflict") else "both person names",
             f"email {lvl(sig.get('email'), 'identical', 'same local part', 'different')}",
             f"phone {lvl(sig.get('phone'), 'identical', 'same last 7 digits', 'different')}",
             f"address {lvl(sig.get('addr'), 'same street and postcode', 'partly the same', 'different')}"]
    gap = sig.get("dob_gap_years")
    parts.append("date of birth missing on one side" if gap is None else
                 "same date of birth" if sig.get("dob") == 1000 else f"dates of birth {gap} years apart")
    return "; ".join(parts)


# ------------------------------------------------------------------ run_matching

def run_matching(db: Db, run_id: str, prob_threshold: float | None = None, adjudicate: bool = True) -> dict:
    """Dedupe candidates, score, vote, adjudicate splits, closure, classify, publish.
    Returns counts and table names only — no row values reach the planner."""
    require_verified("V6")
    from . import adjudicate as adjmod

    t_all = time.perf_counter()
    tag = run_tag(run_id)
    db.run(f"DELETE FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {lit(run_id)}")
    for kind in ("AB", "AA"):
        pairs = ("LEAST(A_ID, B_ID) AS A_ID, GREATEST(A_ID, B_ID) AS B_ID" if kind == "AA" else "A_ID, B_ID")
        db.run(clean(
            "INSERT INTO DRYDOCK.CANDIDATES (RUN_ID, KIND, A_ID, B_ID, BLOCK_RULES) "
            "SELECT RUN_ID, KIND, A_ID, B_ID, GROUP_CONCAT(BLOCK_RULE ORDER BY BLOCK_RULE SEPARATOR ',') FROM ("
            f"SELECT DISTINCT RUN_ID, KIND, {pairs}, BLOCK_RULE FROM DRYDOCK.CANDIDATES_RAW "
            f"WHERE RUN_ID = {lit(run_id)} AND KIND = {lit(kind)}) GROUP BY RUN_ID, KIND, A_ID, B_ID", "er.dedupe"))
    a_tab, b_tab = build_norm(db, run_id)
    n_a = int(db.scalar(f"SELECT COUNT(*) FROM {a_tab}"))
    n_b = int(db.scalar(f"SELECT COUNT(*) FROM {b_tab}"))
    if prob_threshold is None:
        mc = float(db.scalar(f"SELECT COALESCE(MIN_CONFIDENCE, 0) FROM DRYDOCK.RUNS WHERE RUN_ID = {lit(run_id)}") or 0)
        prob_threshold = max(0.72, mc)
    for kind, other in (("AB", b_tab), ("AA", a_tab)):
        t0 = time.perf_counter()
        db.run(clean(panel_sql(run_id, kind, a_tab, other, prob_threshold), "er.panel"))
        per_rule = dict(db.rows(f"SELECT BLOCK_RULE, COUNT(*) FROM DRYDOCK.CANDIDATES_RAW WHERE RUN_ID = {lit(run_id)} "
                                f"AND KIND = {lit(kind)} GROUP BY BLOCK_RULE"))
        cand = int(db.scalar(f"SELECT COUNT(*) FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {lit(run_id)} AND KIND = {lit(kind)}"))
        possible = n_a * n_b if kind == "AB" else n_a * (n_a - 1) // 2
        if cand:
            events.emit("candidates.ready", run_id, None, kind=kind, possible_pairs=possible, candidates=cand,
                        reduction_ratio=round(possible / cand, 1) if cand else 0.0,
                        per_rule={k: int(v) for k, v in per_rule.items()}, ms=round((time.perf_counter() - t0) * 1000, 1))

    # Adjudicate the SPLIT set only.
    splits = db.dicts(f"SELECT PAIR_ID, KIND, SIGNALS FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {lit(run_id)} "
                      "AND PANEL_RESULT = 'SPLIT' ORDER BY PAIR_ID")
    verdicts: dict[int, dict] = {}
    if splits and adjudicate:
        items = [{"pair_id": int(s["PAIR_ID"]), "kind": s["KIND"],
                  "summary": compare_summary(json.loads(s["SIGNALS"]))} for s in splits]
        verdicts = adjmod.adjudicate(db, run_id, items)
        for pid, v in verdicts.items():
            db.run(f"UPDATE DRYDOCK.CANDIDATES SET AI_VERDICT = {lit(v['verdict'])}, AI_SCORE = {lit(v.get('score'))}, "
                   f"ADJUDICATOR = {lit(v['adjudicator'])}, PRECEDENTS_USED = {lit(json.dumps(v.get('precedents', [])))}, "
                   f"RATIONALE = {lit((v.get('rationale') or '')[:4000])} WHERE PAIR_ID = {int(pid)}")
    db.run(f"UPDATE DRYDOCK.CANDIDATES SET VERDICT = CASE WHEN PANEL_RESULT = 'UNANIMOUS_MERGE' THEN 'MERGE' "
           "WHEN PANEL_RESULT = 'SPLIT' AND AI_VERDICT = 'SAME_PERSON' THEN 'MERGE' ELSE 'REJECT' END "
           f"WHERE RUN_ID = {lit(run_id)}")

    # Transitive closure over every MERGE verdict, A<->B and A<->A together.
    merges = [(int(r[0]), r[1], r[2], r[3]) for r in db.rows(
        f"SELECT PAIR_ID, KIND, A_ID, B_ID FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {lit(run_id)} AND VERDICT = 'MERGE'")]
    comp = components(merges)
    big = {pid: c for pid, c in comp.items() if c[1] > 2}
    db.run(f"UPDATE DRYDOCK.CANDIDATES SET COMPONENT_SIZE = 2 WHERE RUN_ID = {lit(run_id)} AND VERDICT = 'MERGE'")
    by_comp: dict[str, list[int]] = defaultdict(list)
    for pid, (cid, _size) in big.items():
        by_comp[cid].append(pid)
    for cid, pids in by_comp.items():
        for i in range(0, len(pids), 500):
            db.run(f"UPDATE DRYDOCK.CANDIDATES SET VERDICT = 'FLAG', COMPONENT_ID = {lit(cid)}, "
                   f"COMPONENT_SIZE = {comp[pids[0]][1]} WHERE PAIR_ID IN ({', '.join(str(p) for p in pids[i:i + 500])})")
    if by_comp:
        events.emit("cluster.flagged", run_id, None, components=len(by_comp),
                    largest=max(comp[p[0]][1] for p in by_comp.values()), pairs_flagged=len(big),
                    examples=[{"component": c, "size": comp[p[0]][1], "pairs": len(p)} for c, p in list(by_comp.items())[:5]])

    # Match classes for the MERGE verdicts.
    db.run(clean(
        "MERGE INTO DRYDOCK.CANDIDATES t USING ("
        f"SELECT c.PAIR_ID, CASE WHEN c.KIND = 'AA' THEN 'INTERNAL_DEDUP' "
        "WHEN s.S_EMAIL = 1000 THEN 'EXACT_EMAIL' "
        "WHEN s.S_PHONE = 1000 AND a.POSTCODE_N = b.POSTCODE_N THEN 'PHONE_ADDRESS' ELSE 'FUZZY_NAME' END AS MC "
        f"FROM DRYDOCK.CANDIDATES c JOIN ({signals_select(a_tab, b_tab)}) s ON s.PAIR_ID = c.PAIR_ID "
        f"JOIN {a_tab} a ON a.ID = c.A_ID LEFT JOIN {b_tab} b ON b.ID = c.B_ID "
        f"WHERE c.RUN_ID = {lit(run_id)} AND c.VERDICT = 'MERGE') x ON (t.PAIR_ID = x.PAIR_ID) "
        "WHEN MATCHED THEN UPDATE SET t.MATCH_CLASS = x.MC", "er.classify"))
    # AA pairs were scored against A_NORM, so the class join above needs A<->A too.
    db.run(f"UPDATE DRYDOCK.CANDIDATES SET MATCH_CLASS = 'INTERNAL_DEDUP' WHERE RUN_ID = {lit(run_id)} "
           "AND KIND = 'AA' AND VERDICT = 'MERGE'")

    publish(db, run_id, tag)
    counts = {r[0]: int(r[1]) for r in db.rows(
        f"SELECT PANEL_RESULT, COUNT(*) FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {lit(run_id)} GROUP BY PANEL_RESULT")}
    per_class = {r[0]: int(r[1]) for r in db.rows(
        f"SELECT MATCH_CLASS, COUNT(*) FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {lit(run_id)} AND VERDICT = 'MERGE' "
        "GROUP BY MATCH_CLASS")}
    per_class["NEW_CUSTOMERS"] = int(db.scalar(f"SELECT COUNT(*) FROM {ER_WORK}.NEW_{tag}"))
    split_by_class = {r[0]: int(r[1]) for r in db.rows(
        f"SELECT MATCH_CLASS, COUNT(*) FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {lit(run_id)} AND VERDICT = 'MERGE' "
        "AND PANEL_RESULT = 'SPLIT' GROUP BY MATCH_CLASS")}
    events.emit("panel.voted", run_id, None, unanimous_merge=counts.get("UNANIMOUS_MERGE", 0),
                split=counts.get("SPLIT", 0), unanimous_reject=counts.get("UNANIMOUS_REJECT", 0),
                per_class={k: {"merge": v, "split": split_by_class.get(k, 0)} for k, v in per_class.items()})
    return {"run_id": run_id, "panel": counts, "per_class": per_class, "split_merges_by_class": split_by_class,
            "flagged_components": len(by_comp), "flagged_pairs": len(big),
            "tables": {"matches": f"{ER_WORK}.MATCHES_{tag}", "new_customers": f"{ER_WORK}.NEW_{tag}",
                       "dedup": f"{ER_WORK}.DEDUP_{tag}"},
            "columns": published_columns(), "ms": round((time.perf_counter() - t_all) * 1000, 1)}


def published_columns() -> dict:
    b = ["B_FULL_NAME", "B_EMAIL", "B_PHONE", "B_ADDR_LINE", "B_CITY", "B_POSTCODE", "B_COUNTRY", "B_DATE_OF_BIRTH",
         "B_LAST_SEEN"]
    return {"matches": ["GOLDEN_ID", "A_ID", "B_ID", "MATCH_CLASS", "PANEL_RESULT", "PAIR_ID", "A_CREATED_AT"] + b,
            "new_customers": ["NEW_GOLDEN_ID", "B_ID"] + b,
            "dedup": ["KEEP_GOLDEN_ID", "DROP_GOLDEN_ID", "A_ID", "A_DUP_ID", "PANEL_RESULT", "PAIR_ID"]}


def publish_sql(run_id: str, tag: str, m: dict) -> list[str]:
    """The three ER_WORK decision tables for a run, as SQL (pure: offline-testable)."""
    bvals = ", ".join(f"{m[f]} AS B_{f}" for f in GOLDEN_FIELDS) + \
        f", {m.get('LAST_SEEN', 'LAST_SEEN')} AS B_LAST_SEEN"
    return [
        f"CREATE OR REPLACE TABLE {ER_WORK}.MATCHES_{tag} AS SELECT 'G-' || c.A_ID AS GOLDEN_ID, c.A_ID, c.B_ID, "
        f"c.MATCH_CLASS, c.PANEL_RESULT, c.PAIR_ID, a.CREATED_AT AS A_CREATED_AT, {bvals} "
        "FROM DRYDOCK.CANDIDATES c JOIN SOURCE_B.CLIENTS ON SOURCE_B.CLIENTS.CLIENT_REF = c.B_ID "
        "JOIN SOURCE_A.CUSTOMERS a ON a.CUST_ID = c.A_ID "
        f"WHERE c.RUN_ID = {lit(run_id)} AND c.KIND = 'AB' AND c.VERDICT = 'MERGE'",
        f"CREATE OR REPLACE TABLE {ER_WORK}.NEW_{tag} AS SELECT 'G-' || CLIENT_REF AS NEW_GOLDEN_ID, "
        f"CLIENT_REF AS B_ID, {bvals} FROM SOURCE_B.CLIENTS WHERE CLIENT_REF NOT IN ("
        f"SELECT B_ID FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {lit(run_id)} AND KIND = 'AB' "
        "AND VERDICT IN ('MERGE', 'FLAG'))",
        f"CREATE OR REPLACE TABLE {ER_WORK}.DEDUP_{tag} AS SELECT 'G-' || A_ID AS KEEP_GOLDEN_ID, "
        "'G-' || B_ID AS DROP_GOLDEN_ID, A_ID, B_ID AS A_DUP_ID, PANEL_RESULT, PAIR_ID "
        f"FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {lit(run_id)} AND KIND = 'AA' AND VERDICT = 'MERGE'",
    ]


def publish(db: Db, run_id: str, tag: str) -> None:
    for sql in publish_sql(run_id, tag, load_mapping(db, run_id)):
        db.run(clean(sql, "er.publish"))


# ------------------------------------------------------------------ diff hooks

def _branch_info(db: Db, bid: str) -> dict | None:
    r = db.dicts(f"SELECT b.RUN_ID, b.DEFECT_CLASS, r.GATE_ENABLED FROM DRYDOCK.BRANCHES b "
                 f"LEFT JOIN DRYDOCK.RUNS r ON r.RUN_ID = b.RUN_ID WHERE b.BRANCH_ID = {lit(bid)}")
    return r[0] if r else None


def expected_keys_sql(tag: str, cls: str) -> str:
    if cls == "NEW_CUSTOMERS":
        return f"SELECT NEW_GOLDEN_ID FROM {ER_WORK}.NEW_{tag}"
    if cls == "INTERNAL_DEDUP":
        return (f"SELECT KEEP_GOLDEN_ID FROM {ER_WORK}.DEDUP_{tag} UNION ALL "
                f"SELECT DROP_GOLDEN_ID FROM {ER_WORK}.DEDUP_{tag}")
    return f"SELECT GOLDEN_ID FROM {ER_WORK}.MATCHES_{tag} WHERE MATCH_CLASS = {lit(cls)}"


def split_keys_sql(tag: str, cls: str) -> str | None:
    if cls == "INTERNAL_DEDUP":
        return (f"SELECT KEEP_GOLDEN_ID FROM {ER_WORK}.DEDUP_{tag} WHERE PANEL_RESULT = 'SPLIT' UNION ALL "
                f"SELECT DROP_GOLDEN_ID FROM {ER_WORK}.DEDUP_{tag} WHERE PANEL_RESULT = 'SPLIT'")
    if cls == "NEW_CUSTOMERS":
        return None
    return (f"SELECT GOLDEN_ID FROM {ER_WORK}.MATCHES_{tag} WHERE MATCH_CLASS = {lit(cls)} "
            "AND PANEL_RESULT = 'SPLIT'")


def default_approvals(db: Db, bid: str, table: str, run_id: str | None) -> dict:
    """Panel SPLIT rows start unchecked (a human must look), and so does any
    changed row the published decisions do not explain. Not applied in the A/B
    control (GATE_ENABLED = FALSE): there, every observed row auto-applies."""
    info = _branch_info(db, bid)
    if not info or info["DEFECT_CLASS"] not in MATCH_CLASSES or not info["RUN_ID"]:
        return {}
    if info["GATE_ENABLED"] is not None and not bool(info["GATE_ENABLED"]):
        return {"needs_review": 0, "control": True}
    tag, cls = run_tag(info["RUN_ID"]), info["DEFECT_CLASS"]
    def mark(reason: str, cond: str) -> int:
        return db.run(clean(
            f"UPDATE DRYDOCK.DIFF_ROWS SET APPROVED = FALSE, DEFAULT_REASON = {lit(reason)} WHERE BRANCH_ID = {lit(bid)} "
            f"AND TARGET_TABLE = {lit(ident(table))} AND DEFAULT_REASON = 'DEFAULT' AND {cond}", "er.defaults"))
    unexplained = mark("UNEXPLAINED", f"KEY_VALUE NOT IN ({expected_keys_sql(tag, cls)})")
    sk = split_keys_sql(tag, cls)
    split = mark("PANEL_SPLIT", f"KEY_VALUE IN ({sk})") if sk else 0
    return {"needs_review": split + unexplained, "panel_split": split, "unexplained": unexplained}


def enrich_cards(db: Db, bid: str, run_id: str | None, cards: list[dict]) -> list[dict]:
    """Attach the pair (A record, B record, signals, votes, AI verdict, precedents)
    to each diff card and order per L9: split pairs, clusters, deletions, rest."""
    info = _branch_info(db, bid)
    if not info or not info["RUN_ID"] or info["DEFECT_CLASS"] not in MATCH_CLASSES or not cards:
        return cards
    rid = info["RUN_ID"]
    keys = [c["key"] for c in cards]
    ids = [k[2:] for k in keys if k.startswith("G-")]
    pairs: dict[str, dict] = {}
    for i in range(0, len(ids), 400):
        chunk = ", ".join(lit(x) for x in ids[i:i + 400])
        for r in db.dicts(
                "SELECT PAIR_ID, KIND, A_ID, B_ID, SIGNALS, SCORE_TOTAL, VOTE_DETERM, VOTE_PROB, VOTE_SKEPTIC, "
                "PANEL_RESULT, AI_VERDICT, AI_SCORE, ADJUDICATOR, PRECEDENTS_USED, VERDICT, MATCH_CLASS, RATIONALE, "
                f"COMPONENT_SIZE FROM DRYDOCK.CANDIDATES WHERE RUN_ID = {lit(rid)} AND VERDICT IN ('MERGE', 'FLAG') "
                f"AND (A_ID IN ({chunk}) OR (KIND = 'AA' AND B_ID IN ({chunk})))"):
            for gid in {f"G-{r['A_ID']}"} | ({f"G-{r['B_ID']}"} if r["KIND"] == "AA" else set()):
                pairs.setdefault(gid, r)
    a_ids = sorted({p["A_ID"] for p in pairs.values()} | {p["B_ID"] for p in pairs.values() if p["KIND"] == "AA"})
    b_ids = sorted({p["B_ID"] for p in pairs.values() if p["KIND"] == "AB"})
    a_rec = _records(db, "SOURCE_A.CUSTOMERS", "CUST_ID", a_ids)
    b_rec = _records(db, "SOURCE_B.CLIENTS", "CLIENT_REF", b_ids)
    prec_ids = sorted({int(x) for p in pairs.values() for x in json.loads(p["PRECEDENTS_USED"] or "[]")})
    precs = {}
    if prec_ids:
        for r in db.dicts("SELECT PRECEDENT_ID, HUMAN_VERDICT, HUMAN_NOTE, DECIDED_BY, CREATED_AT, CONTEXT FROM "
                          f"DRYDOCK.PRECEDENTS WHERE PRECEDENT_ID IN ({', '.join(str(p) for p in prec_ids)})"):
            precs[int(r["PRECEDENT_ID"])] = r
    for c in cards:
        p = pairs.get(c["key"])
        if not p:
            continue
        sig = json.loads(p["SIGNALS"] or "{}")
        c["pair"] = {
            "pair_id": int(p["PAIR_ID"]), "kind": p["KIND"], "a_id": p["A_ID"], "b_id": p["B_ID"],
            "a": a_rec.get(p["A_ID"]), "b": (a_rec if p["KIND"] == "AA" else b_rec).get(p["B_ID"]),
            "signals": sig, "summary": compare_summary(sig), "score": float(p["SCORE_TOTAL"] or 0),
            "votes": {"deterministic": p["VOTE_DETERM"], "probabilistic": p["VOTE_PROB"], "skeptic": p["VOTE_SKEPTIC"]},
            "panel": p["PANEL_RESULT"], "ai_verdict": p["AI_VERDICT"],
            "ai_score": float(p["AI_SCORE"]) if p["AI_SCORE"] is not None else None,
            "adjudicator": p["ADJUDICATOR"], "rationale": p["RATIONALE"], "verdict": p["VERDICT"],
            "component_size": int(p["COMPONENT_SIZE"] or 2),
            "precedents": [{"id": pid, "verdict": precs[pid]["HUMAN_VERDICT"], "note": precs[pid]["HUMAN_NOTE"],
                            "by": precs[pid]["DECIDED_BY"], "at": str(precs[pid]["CREATED_AT"])}
                           for pid in json.loads(p["PRECEDENTS_USED"] or "[]") if int(pid) in precs],
        }

    def order(c):
        p = c.get("pair") or {}
        return (0 if p.get("panel") == "SPLIT" else 1 if (p.get("component_size") or 2) > 2
                else 2 if c["class"] == "DELETED" else 3 if not c.get("approved", True) else 4, c["key"])
    return sorted(cards, key=order)


def _records(db: Db, table: str, key: str, ids: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        if not chunk:
            continue
        for r in db.dicts(f"SELECT * FROM {table} WHERE {key} IN ({', '.join(lit(x) for x in chunk)})"):
            out[r[key]] = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
    return out


def install_hooks() -> None:
    diffmod.HOOKS["defaults"] = default_approvals
    diffmod.HOOKS["enrich"] = enrich_cards
