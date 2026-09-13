"""Fingerprint algebra — what merge.py's independent post-state check relies on."""

from __future__ import annotations

import hashlib
import random

from drydock.hashing import ZERO, Column, Fingerprint, canon, row_hash_expr
from drydock.lintguard import dialect_lint


def _h(i: int) -> str:
    return hashlib.sha256(str(i).encode()).hexdigest()


def test_fingerprint_is_order_independent():
    hs = [_h(i) for i in range(1000)]
    a = Fingerprint.of_hashes(hs)
    random.Random(1).shuffle(hs)
    assert Fingerprint.of_hashes(hs) == a


def test_fingerprint_algebra_matches_recomputation():
    base = [_h(i) for i in range(2000)]
    changed_old, changed_new = base[10:20], [_h(10_000 + i) for i in range(10)]
    deleted, added = base[50:53], [_h(20_000 + i) for i in range(4)]
    post = [h for h in base if h not in changed_old and h not in deleted] + changed_new + added
    pre = Fingerprint.of_hashes(base)
    expected = pre - Fingerprint.of_hashes(changed_old + deleted) + Fingerprint.of_hashes(changed_new + added)
    assert expected == Fingerprint.of_hashes(post)
    assert expected.hex == Fingerprint.of_hashes(post).hex


def test_one_row_change_moves_the_fingerprint():
    base = [_h(i) for i in range(100)]
    moved = base[:-1] + [_h(999)]
    assert Fingerprint.of_hashes(base) != Fingerprint.of_hashes(moved)


def test_zero_identity():
    fp = Fingerprint.of_hashes([_h(1)])
    assert fp + ZERO == fp and fp - fp == ZERO


def test_generated_hash_sql_is_exasol_clean():
    cols = [Column("GOLDEN_ID", "VARCHAR(64) UTF8"), Column("DATE_OF_BIRTH", "DATE"),
            Column("MERGED_AT", "TIMESTAMP"), Column("AMOUNT", "DECIMAL(12,2)"), Column("FLAG", "BOOLEAN")]
    sql = f"SELECT {row_hash_expr(cols)} AS H FROM GOLDEN.CUSTOMERS"
    assert not [f for f in dialect_lint.lint_sql(sql, "t") if f.level == "FAIL"]
    assert "TO_CHAR(DATE_OF_BIRTH, 'YYYY-MM-DD')" in sql
    assert canon("X", "VARCHAR(10) UTF8").startswith("COALESCE(TO_CHAR(LENGTH(X)) || ':' || X")


def test_diff_value_fetch_never_selects_the_key_column_twice():
    """The key is both the key and one of the data columns; the driver refuses duplicate column names,
    so every projected name must be distinct."""
    import sqlglot

    from drydock import diff
    from drydock.hashing import Column, key_text
    cols = [Column("GOLDEN_ID", "VARCHAR(64) UTF8"), Column("CITY", "VARCHAR(100) UTF8"),
            Column("MERGED_AT", "TIMESTAMP")]
    sql = diff._values_sql("GOLDEN", "CUSTOMERS", key_text(cols, "GOLDEN_ID"),
                           [c.name for c in cols], ["G00000001", "G00000002"])
    names = [e.alias_or_name for e in sqlglot.parse_one(sql, read="exasol").expressions]
    assert names[0] == diff.KEY_ALIAS
    assert len(names) == len(set(names)), f"duplicate output column names: {names}"
    assert names[1:] == ["GOLDEN_ID", "CITY", "MERGED_AT"]
