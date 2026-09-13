-- Run as DRYDOCK_SVC (scripts/setup.py does this).
-- SOURCE_A, SOURCE_B and BENCH are created by the admin identity in
-- bench/sql/10_sources.sql, so that they are read-only to DRYDOCK_SVC by grant,
-- not merely by policy.

CREATE SCHEMA IF NOT EXISTS GOLDEN;     -- the reconciled master; only merge.py writes here
CREATE SCHEMA IF NOT EXISTS GOLDEN_V;   -- the agent's read surface: views over GOLDEN
CREATE SCHEMA IF NOT EXISTS DRYDOCK;    -- branches, ops, diffs, merges, candidates, precedents
CREATE SCHEMA IF NOT EXISTS ER_WORK;    -- published per-run match decisions, read-only to branches
