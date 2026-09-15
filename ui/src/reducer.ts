// The UI is a pure function of the event stream. No fetching here, no hidden state:
// replaying the same events always produces the same screen. View state (which branch
// the user clicked, a column filter) lives in components and never feeds back into this.

import type { Branch, DrydockEvent, Narr, State } from "./types";

export const initialState: State = {
  runs: {},
  activeRun: null,
  branches: {},
  order: [],
  focus: null,
  narrative: [],
  golden: { fingerprint: null, rows: null, history: [] },
  scores: {},
  adjudicator: null,
  replay: null,
  illustrative: false,
  minConfidence: 0,
  events: 0,
  system: { snapshot: null },
  recent: [],
};

const fmt = (n: number) => n.toLocaleString("en-GB");

function narr(s: State, n: Narr): Narr[] {
  return [...s.narrative, n].slice(-400);
}

function upsert(s: State, id: string, patch: (b: Branch) => Branch): State {
  const b = s.branches[id];
  if (!b) return s;
  return { ...s, branches: { ...s.branches, [id]: patch(b) } };
}

export function reduce(s: State, e: DrydockEvent): State {
  const p = e.payload;
  const bid = e.branch_id;
  s = { ...s, events: s.events + 1 };
  if (e.type !== "db.snapshot" && e.type !== "__replay__" && e.type !== "__reset__") s = { ...s, recent: [...s.recent, e].slice(-40) };
  switch (e.type) {
    // Not an event: the history this state was built from has been replaced (a (re)connect replays it in full,
    // or the dataset was switched). Start again, keeping only what Exasol last reported.
    case "__reset__":
      return { ...initialState, system: s.system };
    case "db.snapshot":
      return { ...s, system: { snapshot: { ...(p as any), ts: e.ts } } };
    case "__replay__":
      return { ...initialState, replay: (e as any).path ?? "replay", events: 0 };

    case "run.started": {
      const run = { id: e.run_id!, tier: p.tier, gateEnabled: p.gate_enabled, mode: p.mode,
                    minConfidence: p.min_confidence, planFrom: p.plan_from_run ?? null, startedAt: e.ts };
      return { ...s, runs: { ...s.runs, [run.id]: run }, activeRun: run.id, minConfidence: p.min_confidence,
               illustrative: s.illustrative || p.mode === "replay-fixture",
               narrative: narr(s, { ts: e.ts, kind: "run", tone: "info",
                 text: `Run ${run.id} · ${p.mode} · tier ${p.tier} · gate ${p.gate_enabled ? "ON" : "OFF (A/B control)"}` +
                       (p.plan_from_run ? ` · replaying ${p.plan_from_run}'s plan` : "") }) };
    }
    case "run.ended": {
      const r = e.run_id && s.runs[e.run_id];
      const error: string | null = p.status === "OK" ? null : (p.summary?.error ?? null);
      return { ...s, runs: r ? { ...s.runs, [r.id]: { ...r, ended: { status: p.status, error } } } : s.runs,
               narrative: narr(s, { ts: e.ts, kind: "run", tone: p.status === "OK" ? "good" : "bad",
                                    text: `Run ${e.run_id} ended: ${p.status}${error ? ` — ${error}` : ""}` }) };
    }
    case "agent.note":
      return { ...s, illustrative: s.illustrative || /ILLUSTRATIVE/.test(p.text),
               narrative: narr(s, { ts: e.ts, kind: p.phase ?? "note", text: p.text, branchId: bid,
                                    tone: p.phase === "disclosure" ? "disclose" : "info" }) };
    case "profile.found":
      return { ...s, narrative: narr(s, { ts: e.ts, kind: "profile", text: `${p.source}.${p.table}${p.column ? "." + p.column : ""}: ${p.finding}` }) };
    case "mapping.declared":
      return { ...s, mapping: p.mapping, narrative: narr(s, { ts: e.ts, kind: "mapping",
                 text: `A→B mapping declared (${Object.keys(p.mapping).length} fields)${p.rationale ? ": " + p.rationale : ""}` }) };
    case "hypothesis.stated":
      return { ...s, narrative: narr(s, { ts: e.ts, kind: "hypothesis",
                 text: `hypothesis: ${p.match_class}, ~${fmt(p.expected_count)} rows — ${p.rationale}` }) };
    case "candidates.ready":
      return { ...s, candidates: [...(s.candidates ?? []).filter(c => c.kind !== p.kind), p as any],
               narrative: narr(s, { ts: e.ts, kind: "blocking", tone: "good",
                 text: `blocking (${p.kind}): ${fmt(p.possible_pairs)} possible pairs → ${fmt(p.candidates)} candidates, ` +
                       `${(p.ms / 1000).toFixed(1)}s, one query` }) };
    case "panel.voted":
      return { ...s, panel: p as any, narrative: narr(s, { ts: e.ts, kind: "panel",
                 text: `panel: ${fmt(p.unanimous_merge)} unanimous merges · ${fmt(p.split)} split · ${fmt(p.unanimous_reject)} rejected` }) };
    case "adjudicating":
      return { ...s, adjudicator: p.adjudicator, adjudication: { pairs: p.pairs, adjudicator: p.adjudicator },
               narrative: narr(s, { ts: e.ts, kind: "adjudicate",
                 text: `adjudicating ${fmt(p.pairs)} split pairs · ${p.adjudicator}` }) };
    case "adjudicated":
      return { ...s, adjudicator: p.adjudicator, adjudication: p as any, narrative: narr(s, { ts: e.ts, kind: "adjudicate",
                 text: `adjudicated ${fmt(p.pairs)}: ` + Object.entries(p.verdicts).map(([k, v]) => `${v} ${k.toLowerCase().replace("_", " ")}`).join(", ") }) };
    case "precedent.cited":
      return { ...s, narrative: narr(s, { ts: e.ts, kind: "precedent", tone: "info",
                 text: `pair ${p.pair_id}: cited precedent ${p.precedent_ids.map((i: number) => "#" + i).join(", ")} → ${p.verdict}` +
                       (p.notes?.length ? ` — "${p.notes[0]}"` : "") }) };
    case "precedent.recorded":
      return { ...s, narrative: narr(s, { ts: e.ts, kind: "precedent", tone: "good",
                 text: `case law #${p.precedent_id}: ${p.verdict}${p.note ? ` — "${p.note}"` : ""}` }) };
    case "cluster.flagged":
      return { ...s, clusters: p as any, narrative: narr(s, { ts: e.ts, kind: "closure", tone: "warn",
                 text: `${p.components} clusters larger than a pair (largest ${p.largest}) flagged, never merged pairwise` }) };

    case "branch.opened": {
      const b: Branch = { id: bid!, runId: e.run_id, cls: p.defect_class, purpose: p.purpose, status: "OPEN",
                          openedAt: e.ts, ops: [], blocked: [], approvals: {} };
      return { ...s, branches: { ...s.branches, [b.id]: b }, order: [...s.order.filter(x => x !== b.id), b.id],
               focus: b.id, narrative: narr(s, { ts: e.ts, kind: "branch", branchId: bid,
                 text: `opened ${bid} · ${p.defect_class || "branch"}` }) };
    }
    case "branch.materialised":
      return { ...upsert(s, bid!, b => ({ ...b, materialised: p as any })),
               narrative: narr(s, { ts: e.ts, kind: "branch", branchId: bid, tone: "good",
                 text: `${p.table} copied into ${bid}: ${fmt(p.rows)} rows in ${Math.round(p.copy_ms)} ms` }) };
    case "branch.op":
      return { ...upsert(s, bid!, b => ({ ...b, ops: [...b.ops, p as any] })),
               narrative: narr(s, { ts: e.ts, kind: "sql", branchId: bid, tone: p.status === "OK" ? "info" : "bad",
                 text: `${p.status} · ${fmt(p.rows_affected)} rows · ${p.rationale}` }) };
    case "branch.blocked":
      return { ...upsert(s, bid!, b => ({ ...b, blocked: [...b.blocked, p as any] })),
               narrative: narr(s, { ts: e.ts, kind: "blocked", branchId: bid, tone: "bad", text: `refused ${p.code}: ${p.message}` }) };
    case "selfcheck":
      return { ...upsert(s, bid!, b => ({ ...b, selfcheck: p as any })),
               narrative: narr(s, { ts: e.ts, kind: "selfcheck", branchId: bid, tone: p.discrepancy ? "warn" : "good",
                 text: p.discrepancy ? `${p.note}. Lowering confidence ${p.confidence_before} → ${p.confidence_after}.`
                                     : `self-check: observed ${fmt(p.observed)} = expected` }) };
    case "diff.computed": {
      const approvals: Record<string, boolean> = {};
      for (const r of p.rows) approvals[r.key] = r.approved;
      return { ...upsert(s, bid!, b => ({ ...b, diff: { ...(p as any), ts: e.ts }, approvals: { ...approvals },
                                          approvedCount: undefined, total: p.rows_total })),
               focus: bid, narrative: narr(s, { ts: e.ts, kind: "diff", branchId: bid,
                 text: `diff ${bid}: ${fmt(p.added)} added · ${fmt(p.changed)} changed · ${fmt(p.deleted)} deleted` }) };
    }
    case "rows.deselected":
      return upsert(s, bid!, b => {
        const approvals = { ...b.approvals };
        for (const k of p.keys) approvals[k] = p.approved;
        return { ...b, approvals, approvedCount: p.approved_count, total: p.total };
      });
    case "merge.requested":
      return { ...upsert(s, bid!, b => ({ ...b, request: p as any })), focus: bid,
               narrative: narr(s, { ts: e.ts, kind: "merge", branchId: bid,
                 text: `merge requested · conf ${p.confidence.toFixed(2)} · ${fmt(p.total_changed)} rows` }) };
    case "merge.gated": {
      const status = p.decision === "MERGE" ? "OPEN" : p.decision === "PENDING" ? "PENDING" : "BLOCKED";
      return { ...upsert(s, bid!, b => ({ ...b, gate: { ...(p as any), ts: e.ts }, status: status as any })), focus: bid,
               narrative: narr(s, { ts: e.ts, kind: "gate", branchId: bid,
                 tone: p.decision === "MERGE" ? "good" : p.decision === "PENDING" ? "warn" : "bad",
                 text: `gate: ${p.decision} — ${p.reason}` }) };
    }
    case "merge.applied":
      return { ...upsert(s, bid!, b => ({ ...b, applied: p as any, status: "MERGED" })),
               narrative: narr(s, { ts: e.ts, kind: "merge", branchId: bid, tone: "good",
                 text: `merged ${fmt(p.rows_applied)} of ${fmt(p.rows_applied + p.rows_excluded)} rows · swap ${Math.round(p.swap_ms)} ms` }) };
    case "merge.rejected":
      return { ...upsert(s, bid!, b => ({ ...b, rejected: p as any, status: "REJECTED" })),
               narrative: narr(s, { ts: e.ts, kind: "merge", branchId: bid, tone: "bad", text: `rejected ${p.code}${p.note ? ": " + p.note : ""}` }) };
    case "branch.discarded":
    case "branch.expired":
      return { ...upsert(s, bid!, b => ({ ...b, status: b.status === "REJECTED" ? "REJECTED" : e.type === "branch.expired" ? "EXPIRED" : "DISCARDED",
                                          discardedReason: p.reason ?? "TTL expired" })),
               narrative: narr(s, { ts: e.ts, kind: "discard", branchId: bid,
                 text: `${bid} dropped (${p.reason ?? "TTL"}) — GOLDEN untouched` }) };
    case "unmerge.applied":
      return { ...upsert(s, bid!, b => ({ ...b, unmerged: p as any, status: "UNMERGED" })),
               narrative: narr(s, { ts: e.ts, kind: "unmerge", branchId: bid, tone: p.fingerprint_match ? "good" : "bad",
                 text: `unmerged merge ${p.merge_id}: fingerprint ${p.fingerprint_match ? "matches" : "does not match"}` }) };
    case "policy.adapted":
      return { ...s, minConfidence: p.min_confidence_after, narrative: narr(s, { ts: e.ts, kind: "policy", tone: "warn",
                 text: `min confidence ${p.min_confidence_before} → ${p.min_confidence_after} (${p.reason})` }) };
    case "score.updated":
      return { ...s, scores: { ...s.scores, [e.run_id!]: p as any } };
    case "golden.fingerprint": {
      const prev = s.golden.fingerprint;
      const cause = prev && prev !== p.fingerprint ? "merge" : "observed";
      return { ...s, golden: { fingerprint: p.fingerprint, rows: p.rows,
                               history: [...s.golden.history, { ts: e.ts, fingerprint: p.fingerprint, cause }] } };
    }
    case "dataset.activated":
      return { ...s, narrative: narr(s, { ts: e.ts, kind: "data", tone: "info",
                 text: `Now working on ${p.label}: ${fmt(p.rows_a)} records in System A, ${fmt(p.rows_b)} in System B; ` +
                       `GOLDEN starts from ${fmt(p.golden_rows)} rows` + (p.scored ? "" : " (no answer key, so no score)") }) };
    default:
      return s;
  }
}

export function replayAll(events: DrydockEvent[]): State {
  return events.reduce(reduce, initialState);
}
