// Mirrors the event contract in drydock/events.py. tests/test_events.py pins the Python side;
// src/reducer.test.ts replays the fixture through this side.

export interface DrydockEvent {
  type: string;
  ts: string;
  run_id: string | null;
  branch_id: string | null;
  payload: Record<string, any>;
}

export type Rec = Record<string, string | number | null>;

export interface Pair {
  pair_id: number;
  kind: "AB" | "AA";
  a_id: string;
  b_id: string;
  a: Rec | null;
  b: Rec | null;
  signals: Record<string, number | null>;
  summary: string;
  score: number;
  votes: { deterministic: string; probabilistic: string; skeptic: string };
  panel: "UNANIMOUS_MERGE" | "SPLIT" | "UNANIMOUS_REJECT";
  ai_verdict: string | null;
  ai_score: number | null;
  adjudicator: string | null;
  rationale: string | null;
  verdict: string;
  component_size: number;
  precedents: { id: number; verdict: string; note: string | null; by: string | null; at: string }[];
}

export interface Card {
  key: string;
  class: "ADDED" | "CHANGED" | "DELETED";
  cols_changed: string[];
  before: Rec | null;
  after: Rec | null;
  approved: boolean;
  default_reason: string;
  pair?: Pair;
}

export interface Diff {
  table: string;
  mode: "KEYED" | "KEYLESS";
  added: number;
  changed: number;
  deleted: number;
  unchanged: number;
  columns_touched: Record<string, number>;
  column_order: string[];
  rows: Card[];
  rows_total: number;
  base_drifted: boolean;
  diff_ms: number;
  defaults?: { needs_review?: number; panel_split?: number; unexplained?: number; control?: boolean };
  ts: string;
}

export interface Gate {
  merge_id: number;
  decision: "MERGE" | "PENDING" | "BLOCKED";
  reason: string;
  risk: number;
  tier: number;
  limits: {
    max_changed?: number; max_risk?: number; max_delete_pct?: number; hard_delete_pct?: number;
    min_confidence?: number; threshold?: number; delete_pct?: number; total_changed?: number; confidence?: number;
  };
  rows_needing_review: number;
  ts: string;
}

export type BranchStatus = "OPEN" | "PENDING" | "MERGED" | "BLOCKED" | "REJECTED" | "DISCARDED" | "EXPIRED" | "UNMERGED";

export interface Branch {
  id: string;
  runId: string | null;
  cls: string;
  purpose: string;
  status: BranchStatus;
  openedAt: string;
  materialised?: { table: string; rows: number; copy_ms: number; base_fingerprint: string };
  ops: { op_id: number; status: string; rows_affected: number; rationale: string; sql: string; error?: string | null }[];
  blocked: { code: string; message: string; sql: string }[];
  diff?: Diff;
  approvals: Record<string, boolean>;
  approvedCount?: number;
  total?: number;
  request?: { merge_id: number; confidence: number; rationale: string; total_changed: number };
  gate?: Gate;
  applied?: { merge_id: number; rows_applied: number; rows_excluded: number; stage_ms: number; swap_ms: number;
              pre_fingerprint: string; post_fingerprint: string };
  rejected?: { code: string; note?: string | null };
  unmerged?: { fingerprint_match: boolean; restored_fingerprint: string; expected_fingerprint: string };
  selfcheck?: { expected: number; observed: number; discrepancy: number; confidence_before: number;
                confidence_after: number; note: string };
  discardedReason?: string;
}

export interface Score {
  precision: number; recall: number; f1: number; false_merges_in_golden: number; decoy_false_merges: number;
  detail?: Record<string, any>;
}

export interface Narr {
  ts: string;
  kind: string;
  text: string;
  tone?: "info" | "warn" | "good" | "bad" | "disclose";
  branchId?: string | null;
}

export interface RunInfo {
  id: string;
  tier: number;
  gateEnabled: boolean;
  mode: string;
  minConfidence: number;
  planFrom: string | null;
  startedAt: string;
  ended?: { status: string };
}

export interface SnapTable { schema: string; table: string; rows: number | null; kind: "TABLE" | "VIEW" }

/** db.snapshot: what Exasol itself reported at db_time (session, version, row counts, columns). */
export interface Snapshot {
  db_time: string; session: string; version: string; tables: SnapTable[];
  columns: Record<string, [string, string][]>; ms: number; trigger: string; ts: string;
  keys: { schema: string; table: string; column: string; kind: string; ref: string | null }[];
  config: { tiers: { tier: number; label: string; max_changed: number; max_risk: number; allow_deletes: boolean; max_delete_pct: number }[];
            planner_model: string; adjudicator_model: string; adjudicator_rung: string; mcp_row_limit: number; mcp_schema_pattern: string };
}

export interface State {
  runs: Record<string, RunInfo>;
  activeRun: string | null;
  branches: Record<string, Branch>;
  order: string[];
  focus: string | null;
  narrative: Narr[];
  golden: { fingerprint: string | null; rows: number | null; history: { ts: string; fingerprint: string; cause: string }[] };
  scores: Record<string, Score>;
  panel?: { unanimous_merge: number; split: number; unanimous_reject: number; per_class: Record<string, { merge: number; split: number }> };
  candidates?: { kind: string; possible_pairs: number; candidates: number; reduction_ratio: number; ms: number; per_rule: Record<string, number> }[];
  adjudication?: { pairs: number; adjudicator: string; verdicts?: Record<string, number>; latency_ms?: number };
  clusters?: { components: number; largest: number; pairs_flagged: number };
  mapping?: Record<string, string>;
  adjudicator: string | null;
  replay: string | null;
  illustrative: boolean;
  minConfidence: number;
  events: number;
  system: { snapshot: Snapshot | null };
  recent: DrydockEvent[];   // the last 40 events (not db.snapshot), for the System view's live flows and log
}
