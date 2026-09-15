// Pure selectors over the reducer state. Every number the screens show is derived here from
// events the backend emitted — nothing is typed in. If an event has not arrived, the value is
// null and the UI shows "—".

import type { Branch, Card, Pair, Rec, State } from "./types";

// ------------------------------------------------------------------ stepper

export type StepStatus = "done" | "current" | "pending" | "failed";
export interface Step { n: number; label: string; status: StepStatus; note: string }

export function steps(s: State): Step[] {
  const branches = Object.values(s.branches);
  const pending = branches.some((b) => b.status === "PENDING");
  const anyDiff = branches.some((b) => b.diff);
  const anyMerged = branches.some((b) => b.applied);
  const run = s.activeRun ? s.runs[s.activeRun] : null;
  const profiled = s.narrative.some((n) => n.kind === "profile" || n.kind === "mapping");
  const raw: [string, boolean][] = [
    ["Profile Schemas", profiled || !!s.mapping],
    ["Find Candidates", !!s.candidates?.length],
    ["Review Matches", !!s.panel && !pending && anyMerged],
    ["See Diff", anyDiff],
    ["Merge", anyMerged && !pending],
    ["Complete", run?.ended?.status === "OK"],
  ];
  const started = !!run;
  const failed = !!run?.ended && run.ended.status !== "OK";
  let currentSet = false;
  return raw.map(([label, done], i) => {
    let status: StepStatus = done ? "done" : "pending";
    if (!done && !currentSet && started) {
      status = failed ? "failed" : "current";   // a stopped run halts where it got to; it did not complete
      currentSet = true;
    }
    const note = { done: "Completed", current: "In Progress", pending: "Pending", failed: "Stopped" }[status];
    return { n: i + 1, label, status, note };
  });
}

// ------------------------------------------------------------------ KPIs

export interface Kpis {
  candidates: number | null;
  autoMatched: number | null;
  needsReview: number | null;
  rejected: number | null;
  golden: number | null;
  scored: number | null;
}

export function kpis(s: State): Kpis {
  const ab = s.candidates?.find((c) => c.kind === "AB");
  const cand = s.candidates?.length ? s.candidates.reduce((t, c) => t + c.candidates, 0) : null;
  const p = s.panel;
  return {
    candidates: cand ?? (ab ? ab.candidates : null),
    autoMatched: p ? p.unanimous_merge : null,
    needsReview: p ? p.split : null,
    rejected: p ? p.unanimous_reject : null,
    golden: s.golden.rows,
    scored: p ? p.unanimous_merge + p.split + p.unanimous_reject : null,
  };
}

export const pct = (n: number | null, d: number | null) => (n == null || !d ? null : Math.round((n / d) * 100));

// ------------------------------------------------------------------ pairs

// Field alignment is derived from the mapping the agent declared (mapping.declared):
// each golden field's SQL expression is scanned for the SOURCE_B columns it reads.
export const FIELDS = ["FULL_NAME", "EMAIL", "PHONE", "ADDR_LINE", "CITY", "POSTCODE", "DATE_OF_BIRTH", "COUNTRY"] as const;
export const FIELD_LABEL: Record<string, string> = {
  FULL_NAME: "Name", EMAIL: "Email", PHONE: "Phone", ADDR_LINE: "Address", CITY: "City",
  POSTCODE: "Postcode", DATE_OF_BIRTH: "DOB", COUNTRY: "Country",
};

function bColumns(expr: string | undefined, b: Rec | null): string[] {
  if (!expr || !b) return [];
  const words = expr.replace(/'[^']*'/g, " ").match(/[A-Z_][A-Z0-9_]*/g) ?? [];
  const out: string[] = [];
  for (const w of words) if (w in b && !out.includes(w)) out.push(w);
  return out;
}

export function sideValue(field: string, side: "a" | "b", pair: Pair, mapping?: Record<string, string>): string | null {
  const rec = side === "a" ? pair.a : pair.b;
  if (!rec) return null;
  if (side === "a" || pair.kind === "AA") {
    const v = rec[field];
    return v == null ? null : String(v);
  }
  const cols = bColumns(mapping?.[field], rec);
  const vals = cols.map((c) => rec[c]).filter((v) => v != null && v !== "");
  return vals.length ? vals.join(" ") : null;
}

export function norm(field: string, v: unknown): string {
  if (v == null) return "";
  let x = String(v).trim().toLowerCase();
  if (field === "PHONE") return x.replace(/\D/g, "").slice(-10);
  if (field === "DATE_OF_BIRTH") {
    const m = x.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
    if (m) x = `${m[3]}-${m[2]}-${m[1]}`;
    return x.slice(0, 10);
  }
  if (field === "POSTCODE" || field === "ADDR_LINE") return x.replace(/[^a-z0-9]/g, "");
  return x.replace(/\s+/g, " ");
}

export type FieldCmp = { field: string; a: string | null; b: string | null; state: "same" | "diff" | "missing" };

export function compareFields(pair: Pair, mapping?: Record<string, string>): FieldCmp[] {
  const out: FieldCmp[] = [];
  for (const f of FIELDS) {
    const a = sideValue(f, "a", pair, mapping);
    const b = sideValue(f, "b", pair, mapping);
    if (a == null && b == null) continue;
    const state = a == null || b == null ? "missing" : norm(f, a) === norm(f, b) ? "same" : "diff";
    out.push({ field: f, a, b, state });
  }
  return out;
}

// Risk is a RULE over the panel's own outputs, stated here so it can be explained:
//   High   — the three matchers disagreed, or the pair sits in a cluster larger than 2
//   Medium — unanimous, but a negative signal fired (Sr/Jr, business, given-name, DOB conflict) or score < 0.90
//   Low    — unanimous, no negative signal, score >= 0.90
export type Risk = "High" | "Medium" | "Low";

export function risk(p: Pair): Risk {
  if (p.panel === "SPLIT" || p.component_size > 2) return "High";
  const sig = p.signals;
  const conflict = !!(sig.suffix_conflict || sig.business_conflict || sig.given_initial_conflict) || sig.dob === 0;
  return conflict || p.score < 0.9 ? "Medium" : "Low";
}

export type Suggested = "Match" | "Review" | "No match";

export function suggested(p: Pair): Suggested {
  if (p.panel === "SPLIT") return p.ai_verdict === "DIFFERENT_PERSON" ? "No match" : "Review";
  return p.verdict === "MERGE" ? "Match" : "No match";
}

export interface MatchRow {
  key: string;
  branchId: string;
  branchStatus: Branch["status"];
  card: Card;
  pair: Pair;
  approved: boolean;
  risk: Risk;
  suggested: Suggested;
  diffs: number;
}

export function matchRows(s: State): MatchRow[] {
  const out: MatchRow[] = [];
  for (const id of s.order) {
    const b = s.branches[id];
    for (const c of b.diff?.rows ?? []) {
      if (!c.pair) continue;
      const cmp = compareFields(c.pair, s.mapping);
      out.push({ key: c.key, branchId: id, branchStatus: b.status, card: c, pair: c.pair,
                 approved: b.approvals[c.key] ?? c.approved, risk: risk(c.pair), suggested: suggested(c.pair),
                 diffs: cmp.filter((x) => x.state === "diff").length });
    }
  }
  return out;
}

const RISK_ORDER: Record<Risk, number> = { High: 0, Medium: 1, Low: 2 };
export type SortKey = "risk" | "score_desc" | "score_asc";
export type FilterKey = "all" | "review" | "accepted" | "rejected" | "split";

export function filterSort(rows: MatchRow[], q: string, f: FilterKey, sort: SortKey): MatchRow[] {
  const needle = q.trim().toLowerCase();
  let r = rows.filter((x) => {
    if (needle) {
      const hay = [x.pair.a_id, x.pair.b_id, x.key, ...Object.values(x.pair.a ?? {}), ...Object.values(x.pair.b ?? {})]
        .map((v) => String(v ?? "").toLowerCase()).join(" ");
      if (!hay.includes(needle)) return false;
    }
    if (f === "review") return x.risk === "High" || !x.approved;
    if (f === "accepted") return x.approved;
    if (f === "rejected") return !x.approved;
    if (f === "split") return x.pair.panel === "SPLIT";
    return true;
  });
  r = [...r].sort((a, b) =>
    sort === "risk" ? RISK_ORDER[a.risk] - RISK_ORDER[b.risk] || a.pair.score - b.pair.score
      : sort === "score_desc" ? b.pair.score - a.pair.score : a.pair.score - b.pair.score);
  return r;
}

export function signalChips(p: Pair): { label: string; tone: "good" | "bad" | "neutral" }[] {
  const s = p.signals;
  const out: { label: string; tone: "good" | "bad" | "neutral" }[] = [];
  const lvl = (v: number | null | undefined) => (v == null ? null : v / 1000);
  if (s.email === 1000) out.push({ label: "Email match", tone: "good" });
  else if (s.email != null && s.email > 0) out.push({ label: `Email partial (${lvl(s.email)!.toFixed(2)})`, tone: "neutral" });
  if (s.name != null) out.push({ label: `Name similarity (${lvl(s.name)!.toFixed(2)})`, tone: s.name >= 900 ? "good" : "neutral" });
  if (s.phone === 1000) out.push({ label: "Phone match (normalised)", tone: "good" });
  else if (s.phone === 700) out.push({ label: "Phone last 7 digits", tone: "neutral" });
  if (s.addr != null && s.addr >= 800) out.push({ label: "Address match (token)", tone: "good" });
  if (s.dob === 1000) out.push({ label: "Same date of birth", tone: "good" });
  if (s.dob === 0 && s.dob_gap_years != null) out.push({ label: `DOB ${s.dob_gap_years}y apart`, tone: "bad" });
  if (s.suffix_conflict) out.push({ label: "Sr/Jr conflict", tone: "bad" });
  if (s.business_conflict) out.push({ label: "Business vs person", tone: "bad" });
  if (s.given_initial_conflict) out.push({ label: "Given name differs", tone: "bad" });
  return out;
}

// ------------------------------------------------------------------ activity + run

export function activity(s: State, n = 6) {
  return [...s.narrative].reverse().slice(0, n);
}

export function runProgress(s: State) {
  const st = steps(s);
  const done = st.filter((x) => x.status === "done").length;
  return Math.round((done / st.length) * 100);
}

export function elapsedMinutes(s: State): number | null {
  const run = s.activeRun ? s.runs[s.activeRun] : null;
  const last = s.narrative[s.narrative.length - 1];
  if (!run || !last) return null;
  return Math.max(0, Math.round((Date.parse(last.ts) - Date.parse(run.startedAt)) / 60000));
}

// ------------------------------------------------------------------ adjudicator labels
// Every adjudication shows which adjudicator decided it. The label is always rendered, and an unavailable
// adjudicator is visibly different from one that ran.
export type AdjudicatorInfo = { label: string; detail: string; unavailable?: boolean };

export const ADJUDICATORS: Record<string, AdjudicatorInfo> = {
  gemini_fallback: { label: "Gemini", detail: "Adjudicated by Google Gemini (external API). It saw a comparison summary only — never names, emails, phones, addresses or dates." },
  gemini_unavailable: { label: "AI unavailable", detail: "The adjudicator was unavailable (quota or outage), so this pair was left for a person.", unavailable: true },
  exasol_ai: { label: "Exasol AI", detail: "Adjudicated by an in-database model." },
  exasol_ai_precedent: { label: "Exasol AI · precedent", detail: "Adjudicated in the database with precedent retrieval." },
};

export function adjudicatorInfo(rung: string | null | undefined): AdjudicatorInfo | null {
  if (!rung) return null;
  return ADJUDICATORS[rung] ?? { label: rung, detail: `Adjudicator: ${rung}` };
}


// ------------------------------------------------------------------ plain language
// The gate and the backend speak in codes. A person who has never seen Drydock should not have to.

/** One sentence for a gate reason such as "ROWS_NEED_REVIEW (20 panel-split rows)". */
export function explainReason(reason: string | null | undefined): string {
  if (!reason) return "";
  const n = (reason.match(/\(([^)]*)\)/) ?? [])[1] ?? "";
  const code = reason.split(/[ (:]/)[0];
  switch (code) {
    case "WITHIN_TIER": return "Small and safe, so it was merged automatically.";
    case "ROWS_NEED_REVIEW": return `The three matchers disagreed on ${n.replace(" panel-split rows", "") || "some"} rows. Those rows start unticked: look at them in the Diff Viewer, then merge what you agree with.`;
    case "DELETE_PCT": return `This would delete ${n.split(" > ")[0] || "part"} of your list; this tier only deletes up to ${n.split(" > ")[1] || "a small share"} without a person. Check the deleted rows first.`;
    case "DELETE_PCT_EXCEEDED": return "Blocked at every tier: it would delete more than the hard safety limit (5% by default) in one go. Nothing can merge it; split the change instead.";
    case "TOO_MANY_ROWS": return `This changes ${n.split(" > ")[0] || "many"} rows; this tier merges at most ${n.split(" > ")[1] || "a set number"} on its own.`;
    case "RISK": return "Too uncertain for its size: rows changed × how unsure the match is came out above this tier's limit.";
    case "BELOW_MIN_CONFIDENCE": return "Earlier rejections raised the confidence bar, and this change is below it.";
    case "DELETES_NOT_ALLOWED_AT_TIER": return "This tier never deletes rows on its own.";
    case "GATE_DISABLED": return "The gate was switched off for this comparison run, so it merged without checks.";
    case "BASE_DRIFT": return "The official list changed under this copy since it was made. Discard it and run again.";
    case "SCHEMA_CHANGED": return "The table's columns changed since the copy was made, so the change cannot be applied safely.";
    case "NO_DIFF": return "This copy changes nothing.";
    case "STAGING_FINGERPRINT_MISMATCH": return "The staged result did not match what was reviewed, so nothing was written.";
    case "UNACKNOWLEDGED_ERRORS": return "Some statements in this copy failed. It cannot merge until that is dealt with.";
    case "FORBIDDEN_TABLE": return "This copy touched a table Drydock never merges.";
    case "MULTI_TABLE": return "This copy changes more than one table. Drydock merges one table at a time, so each can be checked and undone exactly.";
    default:
      if (code.startsWith("TIER_")) return "This tier never merges on its own: everything waits for a person.";
      if (code.startsWith("BRANCH_")) return "This copy is no longer open, so it cannot be merged.";
      return reason;
  }
}

export interface Explained { title: string; body: string; fix?: string }

/** A backend error message, for a person. */
export function explainError(message: string | null | undefined): Explained {
  const m = message ?? "";
  if (/NotVerified|NOT_VERIFIED|verification not|DRYDOCK_VERIFY_KEY|TAMPERED|altered outside/i.test(m)) {
    return {
      title: "Drydock's safety checks aren't signed for this server",
      body: "Before it changes anything, Drydock proves the database behaves the way it relies on. Those checks are sealed with " +
            "your secret word, and the server that is running cannot open the seal (it was started with a different word, or none).",
      fix: "In the terminal: stop the server (Ctrl-C). In that same terminal type your secret word, run " +
           "scripts/verify.py --only V3,V4,V5,V6,V11, then start the server again. Use one word, in one terminal.",
    };
  }
  if (/Agent mode works on the demo data only/i.test(m)) {
    return { title: "Agent mode is for the demo data", body: m, fix: "Choose 'scripted' in Start New Run." };
  }
  if (/still going/i.test(m)) return { title: "A run is still going", body: m };
  if (/gemini_unavailable|503|UNAVAILABLE|quota/i.test(m)) {
    return { title: "The AI helper (Gemini) is busy", body: "The pairs it would have advised on wait for a person instead.", fix: "Nothing to do: review them in the Merge Gate." };
  }
  if (/Failed to fetch|NetworkError|Load failed/i.test(m)) {
    return { title: "Drydock's server is not reachable", body: "The page cannot reach the orchestrator.", fix: "Check the terminal that runs it, or start it again." };
  }
  return { title: "Something went wrong", body: m };
}

/** Plain words for a readiness check status. */
export function checkWord(status: string): string {
  return { PASS: "passed", FAIL: "failed", UNKNOWN: "not run yet", TAMPERED: "sealed with a different secret word",
           "UNSIGNED-KEY-MISSING": "cannot be read: this server has no secret word" }[status] ?? status.toLowerCase();
}
