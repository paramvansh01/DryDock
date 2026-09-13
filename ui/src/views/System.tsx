// Live System: how Drydock reaches Exasol, drawn from the event stream and from what Exasol itself reports.
//
// A dot travels a connector only when a real event arrives on the stream
// (events older than 30 s — i.e. history replayed to a new browser — never animate), and it carries that
// event's own numbers. Row counts, columns, session and the DB clock come from db.snapshot, which the
// orchestrator re-reads from Exasol about a second after anything happens. The UI still never talks to
// Exasol directly: this view is a pure function of the stream, like every other.

import { AnimatePresence, motion } from "framer-motion";
import {
  Archive, Bot, Database, Gem, GitBranch, KeyRound, Layers, Link2, Lock, Plug, RefreshCw, ScrollText, Server,
  Eye, Settings2, ShieldCheck, Sparkles, Table2, UserRound, Workflow, X,
} from "lucide-react";
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { adjudicatorInfo } from "../derive";
import type { Branch, DrydockEvent, Snapshot, State } from "../types";
import { CountUp, fmt, short } from "../ui";

// ------------------------------------------------------------------ palette
const C = {
  rows: "#2563eb", ai: "#7c3aed", review: "#d97706", merge: "#16a34a", bad: "#dc2626",
  ctl: "#1e3a5f", read: "#0d9488", idle: "#94a3b8",
};

// ------------------------------------------------------------------ geometry (virtual canvas, scaled to fit)
export const W = 1360, H = 846;
type P = [number, number];
type Rect = { x: number; y: number; w: number; h: number };

const R: Record<string, Rect> = {
  human: { x: 18, y: 70, w: 204, h: 100 },
  driver: { x: 18, y: 330, w: 204, h: 110 },
  gemini: { x: 18, y: 596, w: 204, h: 118 },
  orch: { x: 262, y: 60, w: 210, h: 246 },
  dmcp: { x: 262, y: 350, w: 210, h: 104 },
  xmcp: { x: 262, y: 590, w: 210, h: 104 },
  exasol: { x: 540, y: 18, w: 796, h: 818 },
  golden: { x: 564, y: 100, w: 220, h: 184 },
  branches: { x: 564, y: 360, w: 220, h: 240 },
  erwork: { x: 564, y: 660, w: 220, h: 150 },
  control: { x: 828, y: 100, w: 224, h: 710 },
  srcA: { x: 1096, y: 100, w: 216, h: 190 },
  srcB: { x: 1096, y: 330, w: 216, h: 190 },
  bench: { x: 1096, y: 610, w: 216, h: 200 },
};

// The control-plane rows are the DRYDOCK tables Exasol reports in the latest snapshot — nothing else. ORDER only
// sorts them into pipeline order; a table Exasol does not report is not drawn, and an unknown one is appended.
const ORDER = ["RUNS", "MAPPINGS", "CANDIDATES_RAW", "CANDIDATES", "ADJUDICATIONS", "PRECEDENTS", "BRANCHES",
  "MATERIALISATIONS", "BASE_HASHES", "BRANCH_OPS", "DIFFS", "DIFF_ROWS", "MERGES", "IDEMPOTENCY", "EVENTS",
  "TIER_CONFIG", "TABLE_KEYS"];
const ROW_TOP = 160, ROW_BOTTOM = 800, CTL_HEADER_Y = 128;
const rank = (t: string) => (ORDER.includes(t) ? ORDER.indexOf(t) : 999);
export function controlRows(snap: Snapshot | null): string[] {
  return (snap?.tables ?? []).filter((t) => t.schema === "DRYDOCK").map((t) => t.table)
    .sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}
const rowH = (n: number) => (n ? Math.min(36, Math.floor((ROW_BOTTOM - ROW_TOP) / n)) : 36);
const rowY = (rows: string[], t: string) => {
  const i = rows.indexOf(t);
  return i < 0 ? null : ROW_TOP + rowH(rows.length) * i + rowH(rows.length) / 2;
};
const TRUNK = 806;

const E: Record<string, P[]> = {
  human_orch: [[222, 120], [262, 120]],
  driver_orch: [[222, 372], [238, 372], [238, 240], [262, 240]],
  driver_dmcp: [[222, 398], [262, 398]],
  driver_xmcp: [[222, 420], [246, 420], [246, 630], [262, 630]],
  gemini_orch: [[222, 640], [230, 640], [230, 272], [262, 272]],
  gemini_dmcp: [[222, 664], [254, 664], [254, 432], [262, 432]],
  orch_golden: [[472, 150], [564, 150]],
  orch_branches: [[472, 246], [518, 246], [518, 420], [564, 420]],
  dmcp_branches: [[472, 396], [564, 396]],
  xmcp_golden: [[472, 612], [534, 612], [534, 206], [564, 206]],
  xmcp_srcA: [[472, 648], [512, 648], [512, 824], [1074, 824], [1074, 196], [1096, 196]],
  xmcp_srcB: [[472, 670], [500, 670], [500, 830], [1066, 830], [1066, 424], [1096, 424]],
  golden_branches: [[632, 284], [632, 360]],
  branches_golden: [[716, 360], [716, 284]],
  erwork_branches: [[674, 660], [674, 600]],
  srcA_raw: [[1096, 170], [1084, 170], [1084, 244], [1052, 244]],
  srcB_raw: [[1096, 440], [1084, 440], [1084, 256], [1052, 256]],
};
const JOIN: Record<string, P[]> = {   // how each place reaches the control-plane trunk
  orch: [[472, 200], [506, 200], [506, 320], [TRUNK, 320]],
  dmcp: [[472, 432], [526, 432], [526, 630], [TRUNK, 630]],
  golden: [[784, 250], [TRUNK, 250]],
  branches: [[784, 574], [TRUNK, 574]],
  erwork: [[784, 700], [TRUNK, 700]],
};
// A path to one control-plane table. If Exasol has not reported that table (yet), it ends at the card's header.
const ctlPath = (rows: string[], from: keyof typeof JOIN, row: string): P[] => {
  const y = rowY(rows, row) ?? CTL_HEADER_Y;
  return [...JOIN[from], [TRUNK, y], [828, y]];
};
const erworkPath = (rows: string[]): P[] => {
  const y = rowY(rows, "CANDIDATES") ?? CTL_HEADER_Y;
  return [[828, y], [TRUNK, y], [TRUNK, 700], [784, 700]];
};

function clean(pts: P[]): P[] {
  const out: P[] = [];
  for (const p of pts) {
    const q = out[out.length - 1];
    if (q && q[0] === p[0] && q[1] === p[1]) continue;
    const o = out[out.length - 2];
    if (o && q && ((o[0] === q[0] && q[0] === p[0]) || (o[1] === q[1] && q[1] === p[1]))) out.pop();  // collinear
    out.push(p);
  }
  return out;
}

function pathOf(raw: P[], r = 11): string {
  const pts = clean(raw);
  let d = `M ${pts[0][0]} ${pts[0][1]}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [x0, y0] = pts[i - 1], [x1, y1] = pts[i], [x2, y2] = pts[i + 1];
    const d1 = Math.hypot(x1 - x0, y1 - y0), d2 = Math.hypot(x2 - x1, y2 - y1);
    const rr = Math.min(r, d1 / 2, d2 / 2);
    d += ` L ${x1 - ((x1 - x0) / d1) * rr} ${y1 - ((y1 - y0) / d1) * rr} Q ${x1} ${y1} ${x1 + ((x2 - x1) / d2) * rr} ${y1 + ((y2 - y1) / d2) * rr}`;
  }
  const last = pts[pts.length - 1];
  return `${d} L ${last[0]} ${last[1]}`;
}
const lenOf = (pts: P[]) => pts.slice(1).reduce((a, p, i) => a + Math.hypot(p[0] - pts[i][0], p[1] - pts[i][1]), 0);
function pointAt(pts: P[], f: number): P {
  let left = lenOf(pts) * f;
  for (let i = 1; i < pts.length; i++) {
    const seg = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    if (left <= seg) return [pts[i - 1][0] + ((pts[i][0] - pts[i - 1][0]) * left) / (seg || 1), pts[i - 1][1] + ((pts[i][1] - pts[i - 1][1]) * left) / (seg || 1)];
    left -= seg;
  }
  return pts[pts.length - 1];
}
const rev = (p: P[]): P[] => [...p].reverse();

// ------------------------------------------------------------------ events -> flows (real payloads only)
export type Flow = { hops: P[][]; color: string; label?: string; nodes: string[]; rows?: string[] };

const runMode = (e: DrydockEvent, s: State) => (e.run_id && s.runs[e.run_id]?.mode) || (s.activeRun && s.runs[s.activeRun]?.mode) || "scripted";

export function flowsFor(e: DrydockEvent, s: State): Flow[] {
  const p = e.payload || {};
  const agent = runMode(e, s) === "agent";
  const via: "dmcp" | "orch" = agent ? "dmcp" : "orch";
  const drv = agent ? E.driver_dmcp : E.driver_orch;
  const inBranch = agent ? E.dmcp_branches : E.orch_branches;
  const gem = agent ? E.gemini_dmcp : E.gemini_orch;
  const b: Branch | undefined = e.branch_id ? s.branches[e.branch_id] : undefined;
  const rows = controlRows(s.system.snapshot);
  const ctl = (from: keyof typeof JOIN, row: string) => ctlPath(rows, from, row);
  const toErwork = erworkPath(rows);
  switch (e.type) {
    case "run.started":
      return [{ hops: [drv], color: C.ctl, nodes: ["driver", via], rows: ["RUNS"],
        label: `run ${e.run_id} · ${p.mode} · tier ${p.tier} · gate ${p.gate_enabled ? "on" : "off"}` },
        { hops: [ctl(via, "RUNS")], color: C.ctl, nodes: ["control"], rows: ["RUNS"] }];
    case "mapping.declared":
      return [{ hops: [drv, ctl(via, "MAPPINGS")], color: C.ctl, nodes: ["driver", via, "control"], rows: ["MAPPINGS"],
        label: `mapping declared · ${Object.keys(p.mapping || {}).length} fields` }];
    case "profile.found":
      return [{ hops: [rev(p.source === "SOURCE_B" ? E.xmcp_srcB : E.xmcp_srcA), rev(E.driver_xmcp)], color: C.read,
        nodes: [p.source === "SOURCE_B" ? "srcB" : "srcA", "xmcp", "driver"], label: `profile · ${String(p.finding).slice(0, 48)}` }];
    case "agent.note":
      if (p.phase === "disclosure")
        return [{ hops: [rev(E.xmcp_srcA), rev(E.driver_xmcp)], color: C.read, nodes: ["xmcp", "driver"], label: String(p.text).slice(0, 60) }];
      return [{ hops: [], color: C.ai, nodes: ["driver"] }];
    case "candidates.ready":
      return [
        { hops: [E.srcA_raw], color: C.ai, nodes: ["srcA", "control"], rows: ["CANDIDATES_RAW"],
          label: `${fmt(p.candidates)} candidate pairs of ${fmt(p.possible_pairs)} possible · ${Math.round(p.ms)} ms` },
        { hops: [E.srcB_raw], color: C.ai, nodes: ["srcB"], rows: ["CANDIDATES_RAW"] }];
    case "panel.voted":
      return [{ hops: [toErwork], color: C.ai, nodes: ["control", "erwork"], rows: ["CANDIDATES"],
        label: `3 SQL matchers: ${fmt(p.unanimous_merge)} agree · ${fmt(p.split)} split · ${fmt(p.unanimous_reject)} reject` }];
    case "adjudicating":
      return [{ hops: [rev(ctl(via, "CANDIDATES")), rev(gem)], color: C.ai, nodes: ["control", via, "gemini"], rows: ["CANDIDATES"],
        label: `${fmt(p.pairs)} split pairs → ${adjudicatorInfo(p.adjudicator)?.label ?? p.adjudicator} · summaries only` }];
    case "adjudicated": {
      const v = p.verdicts || {};
      return [{ hops: [gem, ctl(via, "ADJUDICATIONS")], color: C.ai, nodes: ["gemini", via, "control"], rows: ["ADJUDICATIONS"],
        label: `${fmt(v.SAME_PERSON ?? 0)} same · ${fmt(v.DIFFERENT_PERSON ?? 0)} different · ${fmt(v.INSUFFICIENT_EVIDENCE ?? 0)} unsure` }];
    }
    case "precedent.recorded":
      return [{ hops: [E.human_orch, ctl("orch", "PRECEDENTS")], color: C.review, nodes: ["human", "orch", "control"], rows: ["PRECEDENTS"],
        label: `precedent #${p.precedent_id} recorded · ${p.verdict}` }];
    case "cluster.flagged":
      return [{ hops: [], color: C.review, nodes: ["control"], rows: ["CANDIDATES"] }];
    case "branch.opened":
      return [{ hops: [drv, ctl(via, "BRANCHES")], color: C.ctl, nodes: ["driver", via, "control", "branches"], rows: ["BRANCHES"],
        label: `CREATE SCHEMA ${p.schema} · ${p.defect_class}` }];
    case "branch.materialised":
      return [{ hops: [E.golden_branches], color: C.rows, nodes: ["golden", "branches"], rows: ["MATERIALISATIONS", "BASE_HASHES"],
        label: `copy-on-write · ${fmt(p.rows)} rows · ${Math.round(p.copy_ms)} ms` }];
    case "branch.op": {
      const verb = String(p.sql || "").trim().split(/\s+/)[0]?.toUpperCase() || "SQL";
      const ok = p.status === "OK";
      const hops = [drv, inBranch];
      const f: Flow[] = [{ hops, color: ok ? C.rows : C.bad, nodes: ["driver", via, "branches"], rows: ["BRANCH_OPS"],
        label: `${verb} in ${e.branch_id} · ${fmt(p.rows_affected)} rows${ok ? "" : ` · ${p.status}`}` }];
      if (/ER_WORK\./i.test(p.sql || "")) f.push({ hops: [E.erwork_branches], color: C.ai, nodes: ["erwork"] });
      return f;
    }
    case "branch.blocked":
      return [{ hops: [drv, inBranch], color: C.bad, nodes: ["driver", via, "branches"], label: `blocked by the firewall · ${p.code}` }];
    case "selfcheck":
      return [{ hops: [], color: p.discrepancy ? C.review : C.ai, nodes: ["driver"] }];
    case "diff.computed":
      return [
        { hops: [ctl("branches", "DIFF_ROWS")], color: C.rows, nodes: ["branches", "control"], rows: ["DIFFS", "DIFF_ROWS"],
          label: `row-level diff · ${fmt(p.changed)} changed · ${fmt(p.added)} added · ${fmt(p.deleted)} deleted · ${Math.round(p.diff_ms)} ms` },
        { hops: [ctl("golden", "DIFF_ROWS")], color: C.rows, nodes: ["golden"] }];
    case "merge.requested":
      return [{ hops: [drv], color: C.review, nodes: ["driver", via, "orch"], rows: ["MERGES"],
        label: `merge request #${p.merge_id} · ${fmt(p.total_changed)} rows · confidence ${Number(p.confidence).toFixed(2)}` }];
    case "merge.gated": {
      const col = p.decision === "MERGE" ? C.merge : p.decision === "PENDING" ? C.review : C.bad;
      const f: Flow[] = [{ hops: [], color: col, nodes: ["orch"], label: undefined }];
      if (p.decision === "PENDING")
        f.push({ hops: [rev(E.human_orch)], color: C.review, nodes: ["orch", "human"], label: `gate held #${p.merge_id} for a person · ${p.reason}` });
      else f.push({ hops: [ctl("orch", "MERGES")], color: col, nodes: ["orch", "control"], rows: ["MERGES"], label: `gate: ${p.decision} · ${p.reason}` });
      return f;
    }
    case "rows.deselected":
      return [{ hops: [E.human_orch, ctl("orch", "DIFF_ROWS")], color: C.review, nodes: ["human", "orch", "control"], rows: ["DIFF_ROWS"],
        label: `${fmt(p.approved_count)} of ${fmt(p.total)} rows selected` }];
    case "merge.applied": {
      const byHuman = b?.gate?.decision === "PENDING";
      const f: Flow[] = [];
      if (byHuman) f.push({ hops: [E.human_orch], color: C.review, nodes: ["human", "orch"], label: "approved by a person" });
      f.push({ hops: [E.branches_golden], color: C.merge, nodes: ["branches", "golden"], rows: ["MERGES"],
        label: `stage-then-swap · ${fmt(p.rows_applied)} rows · stage ${Math.round(p.stage_ms)} ms · swap ${Math.round(p.swap_ms)} ms` });
      return f;
    }
    case "merge.rejected":
      return [{ hops: [E.human_orch], color: C.bad, nodes: ["human", "orch"], rows: ["MERGES", "PRECEDENTS"], label: `rejected #${p.merge_id} · ${p.code}` }];
    case "branch.discarded":
      return [{ hops: [inBranch], color: C.bad, nodes: [via, "branches"], rows: ["BRANCHES"],
        label: `DROP SCHEMA ${e.branch_id} CASCADE${p.drop_ms != null ? ` · ${Math.round(p.drop_ms)} ms` : ""}` }];
    case "branch.expired":
      return [{ hops: [E.orch_branches], color: C.bad, nodes: ["orch", "branches"], rows: ["BRANCHES"], label: `${e.branch_id} expired (idle)` }];
    case "unmerge.applied":
      return [{ hops: [E.human_orch, E.orch_golden], color: p.fingerprint_match ? C.merge : C.bad, nodes: ["human", "orch", "golden"],
        rows: ["MERGES"], label: `unmerge #${p.merge_id} · fingerprint ${p.fingerprint_match ? "matches" : "does not match"}` }];
    case "policy.adapted":
      return [{ hops: [], color: C.review, nodes: ["orch"] }];
    case "score.updated":
      return [{ hops: [], color: C.ctl, nodes: ["bench", "orch"] }];
    case "golden.fingerprint":
      return [{ hops: [rev(E.orch_golden)], color: C.ctl, nodes: ["golden", "orch"], label: `GOLDEN fingerprint ${short(p.fingerprint)} · ${fmt(p.rows)} rows` }];
    case "run.ended":
      return [{ hops: [], color: C.ctl, nodes: ["driver", via] }];
    default:
      return [];
  }
}

function describe(e: DrydockEvent, s: State): string {
  const f = flowsFor(e, s).find((x) => x.label);
  if (f?.label) return f.label;
  const p = e.payload || {};
  switch (e.type) {
    case "agent.note": return String(p.text).slice(0, 110);
    case "hypothesis.stated": return `hypothesis · ${p.match_class}: expect ${fmt(p.expected_count)} rows`;
    case "selfcheck": return `self-check · expected ${fmt(p.expected)}, observed ${fmt(p.observed)}`;
    case "cluster.flagged": return `${fmt(p.pairs_flagged)} pairs in clusters larger than 2 → flagged, never merged`;
    case "score.updated": return `scored against the answer key · F1 ${Number(p.f1).toFixed(4)} · false merges in GOLDEN ${p.false_merges_in_golden}`;
    case "run.ended": return `run ${e.run_id} ended · ${p.status}`;
    case "merge.gated": return `gate: ${p.decision} · ${p.reason}`;
    case "precedent.cited": return `precedent cited for pair ${p.pair_id}`;
    case "policy.adapted": return `policy tightened · min confidence ${p.min_confidence_before} → ${p.min_confidence_after}`;
    default: return e.type;
  }
}

// ------------------------------------------------------------------ keys: only what Exasol reports (db.snapshot.keys)
export function keysOf(snap: Snapshot | null, fq: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const k of snap?.keys ?? []) if (`${k.schema}.${k.table}` === fq) out[k.column] = k.ref ? `${k.kind} → ${k.ref}` : k.kind;
  return out;
}
// ------------------------------------------------------------------ the view
type Dot = { k: string; d: string; dur: number; delay: number; color: string; size: number; op: number };
type Lbl = { k: string; x: number; y: number; text: string; color: string };
type Glow = { k: string; d: string; color: string };
type Sel = { kind: "node"; id: string } | { kind: "table"; fq: string } | null;

export function SystemView({ s, canAct }: { s: State; canAct: boolean }) {
  const wrap = useRef<HTMLDivElement>(null);
  const [k, setK] = useState(1);
  const [dots, setDots] = useState<Dot[]>([]);
  const [labels, setLabels] = useState<Lbl[]>([]);
  const [glows, setGlows] = useState<Glow[]>([]);
  const [hot, setHot] = useState<Record<string, { c: string; n: number }>>({});
  const [hotRows, setHotRows] = useState<Record<string, string>>({});
  const [speed, setSpeed] = useState(1);
  const [sel, setSel] = useState<Sel>(null);
  const [now, setNow] = useState(Date.now());
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const seq = useRef(0);
  const lastSeen = useRef<DrydockEvent | null | undefined>(undefined);
  const lastEventAt = useRef<number | null>(null);
  const snapSeen = useRef<string | null | undefined>(undefined);
  const reduce = useMemo(() => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches, []);

  useLayoutEffect(() => {
    const el = wrap.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setK(el.clientWidth / W));
    ro.observe(el);
    setK(el.clientWidth / W);
    return () => ro.disconnect();
  }, []);
  useEffect(() => { const t = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(t); }, []);

  const play = (f: Flow) => {
    const id = ++seq.current;
    const nd: Dot[] = [], ng: Glow[] = [];
    let t = 0, lbl: Lbl | null = null, best = -1;
    f.hops.forEach((pts, h) => {
      const c = clean(pts), d = pathOf(c), len = lenOf(c);
      const dur = (0.55 + len / 850) / speed;
      [8, 6, 5, 4].forEach((size, i) => nd.push({ k: `${id}-${h}-${i}`, d, dur, delay: t + (i * 0.1) / speed, color: f.color, size, op: [1, 0.8, 0.6, 0.4][i] }));
      ng.push({ k: `${id}-g${h}`, d, color: f.color });
      if (f.label && len > best) { best = len; const [x, y] = pointAt(c, 0.5); lbl = { k: `${id}-l`, x, y, text: f.label, color: f.color }; }
      t += dur * 0.78;
    });
    if (f.label && !lbl) {
      const r = R[f.nodes[0]] ?? R.orch;
      lbl = { k: `${id}-l`, x: r.x + r.w / 2, y: r.y - 12, text: f.label, color: f.color };
    }
    const life = (t + 0.9 / speed) * 1000 + 1800;
    if (!reduce) setDots((x) => (x.length > 90 ? x : [...x, ...nd]));
    setGlows((x) => [...x, ...ng]);
    if (lbl) setLabels((x) => [...x.slice(-5), lbl!]);
    setHot((x) => { const y = { ...x }; f.nodes.forEach((n) => { y[n] = { c: f.color, n: id }; }); return y; });
    if (f.rows) setHotRows((x) => { const y = { ...x }; f.rows!.forEach((r) => { y[r] = f.color; }); return y; });
    window.setTimeout(() => {
      setDots((x) => x.filter((d) => !d.k.startsWith(`${id}-`)));
      setGlows((x) => x.filter((g) => !g.k.startsWith(`${id}-`)));
      setLabels((x) => x.filter((l) => l.k !== `${id}-l`));
      setHot((x) => { const y = { ...x }; f.nodes.forEach((n) => { if (y[n]?.n === id) delete y[n]; }); return y; });
      if (f.rows) setHotRows((x) => { const y = { ...x }; f.rows!.forEach((r) => { if (y[r] === f.color) delete y[r]; }); return y; });
    }, life);
  };

  // New events only: whatever arrived before this view was opened (or is older than 30 s) never animates.
  useEffect(() => {
    const rec = s.recent;
    if (lastSeen.current === undefined) { lastSeen.current = rec[rec.length - 1] ?? null; return; }
    const idx = lastSeen.current ? rec.lastIndexOf(lastSeen.current) : -1;
    const fresh = idx >= 0 ? rec.slice(idx + 1) : rec;
    lastSeen.current = rec[rec.length - 1] ?? null;
    fresh.forEach((e, i) => {
      if (Date.now() - Date.parse(e.ts) > 30_000) return;
      lastEventAt.current = Date.now();
      window.setTimeout(() => flowsFor(e, s).forEach(play), i * 140);
    });
  }, [s.recent]); // eslint-disable-line react-hooks/exhaustive-deps

  const snap = s.system.snapshot;
  const ctlRows = controlRows(snap);
  const cfg = snap?.config;
  useEffect(() => {
    if (snapSeen.current === undefined) { snapSeen.current = snap?.ts ?? null; return; }
    if (snap && snap.ts !== snapSeen.current) {
      snapSeen.current = snap.ts;
      if (Date.now() - Date.parse(snap.ts) > 10_000) return;      // replayed on connect, not new: never animate
      setHot((x) => ({ ...x, exasol: { c: C.ctl, n: ++seq.current } }));
      if (snap.trigger === "refresh")
        play({ hops: [E.orch_golden, rev(E.orch_golden)], color: C.ctl, nodes: ["orch", "exasol"],
          label: `read Exasol catalogue · ${snap.tables.length} tables · ${snap.ms} ms · session …${snap.session.slice(-6)}` });
    }
  }, [snap?.ts]); // eslint-disable-line react-hooks/exhaustive-deps

  const rows = (fq: string) => snap?.tables.find((t) => `${t.schema}.${t.table}` === fq)?.rows ?? null;
  const cols = (fq: string) => snap?.columns[fq] ?? [];
  const run = s.activeRun ? s.runs[s.activeRun] : null;
  const agent = run?.mode === "agent";
  const liveSchemas = new Set((snap?.tables ?? []).filter((t) => t.schema.startsWith("BR_")).map((t) => t.schema));
  const archives = (snap?.tables ?? []).filter((t) => t.schema === "GOLDEN" && t.table.includes("__ARCH_"));
  const erTables = (snap?.tables ?? []).filter((t) => t.schema === "ER_WORK");
  const branchList = [...s.order].reverse().map((id) => s.branches[id]).filter(Boolean);
  const humanMerges = Object.values(s.branches).filter((b) => b.applied && b.gate?.decision === "PENDING").length;
  const adj = s.adjudication;
  const ago = (ts?: string | null) => (ts ? `${Math.max(0, Math.round((now - Date.parse(ts)) / 1000))} s ago` : "—");
  const lastEvent = s.recent[s.recent.length - 1];
  const idle = !lastEvent || now - Date.parse(lastEvent.ts) > 15_000;

  const refresh = async () => {
    setBusy(true); setErr(null);
    try { await api.snapshot(); } catch (e) {
      const m = String(e);
      setErr(/not found|404/i.test(m)
        ? "This orchestrator was started before the Live System update, so it has no refresh endpoint and sends no Exasol snapshots. Restart it (Ctrl+C, then uv run uvicorn drydock.orchestrator:app --port 8765) and reload this page."
        : m);
    }
    setBusy(false);
  };

  const node = (id: string) => ({ hot: hot[id], onClick: () => setSel({ kind: "node", id }), selected: sel?.kind === "node" && sel.id === id });

  return (
    <div className="space-y-4">
      {/* header */}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.16em] text-mist"><Workflow className="h-3.5 w-3.5" /> Architecture · live</div>
          <h1 className="mt-1 text-3xl font-extrabold tracking-tight text-fog">Live System</h1>
          <p className="mt-1 text-[14px] text-fog/75">How Drydock reaches Exasol. Every moving dot is a real event from the backend, drawn the instant it happens; every number is what Exasol just reported.</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex overflow-hidden rounded-lg border border-rule bg-hull text-[12px]">
            {[1, 0.5].map((v) => (
              <button key={v} onClick={() => setSpeed(v)} className={`px-3 py-2 ${speed === v ? "bg-navy text-white" : "text-fog/80 hover:bg-deck"}`}>{v === 1 ? "Real speed" : "Slow motion"}</button>
            ))}
          </div>
          <button onClick={refresh} disabled={busy || !canAct} title={canAct ? "Re-read Exasol now: session, clock, row counts" : "Needs the live orchestrator"}
            className="flex items-center gap-2 rounded-lg bg-navy px-4 py-2 text-[13px] font-semibold text-white shadow-sm disabled:opacity-40">
            <RefreshCw className={`h-4 w-4 ${busy ? "animate-spin" : ""}`} /> Refresh from Exasol
          </button>
        </div>
      </div>
      {err && <div className="rounded-lg border border-flare/30 bg-flare/5 px-3 py-2 text-[13px] text-flare">{err}</div>}

      <Legend />

      {/* canvas */}
      <div className="card relative overflow-hidden">
        <div ref={wrap} className="dd-grid relative w-full" style={{ height: H * k }}>
          <div className="absolute left-0 top-0" style={{ width: W, height: H, transform: `scale(${k})`, transformOrigin: "top left" }}>
            <ColumnLabel x={18} text="Who acts" />
            <ColumnLabel x={262} text="How they reach Exasol" />

            {/* Exasol container */}
            <div className={`absolute rounded-[22px] border ${hot.exasol ? "border-tide/40" : "border-[#d7e0ec]"} transition-colors duration-700`}
              style={{ left: R.exasol.x, top: R.exasol.y, width: R.exasol.w, height: R.exasol.h,
                background: "linear-gradient(180deg, rgba(239,246,255,.85) 0%, rgba(255,255,255,.92) 22%, rgba(255,255,255,.92) 100%)" }}>
              <button onClick={() => setSel({ kind: "node", id: "exasol" })} className="flex w-full items-center gap-3 px-6 pt-4 text-left">
                <IconTile icon={Database} color={C.ctl} size={34} />
                <div className="leading-tight">
                  <div className="text-[15px] font-bold tracking-tight text-fog">Exasol</div>
                  <div className="text-[11.5px] text-mist">{snap ? `version ${snap.version}` : canAct ? "no snapshot yet — if this persists, restart the orchestrator" : "waiting for the orchestrator"}</div>
                </div>
                <div className="ml-auto flex items-center gap-2 text-[11.5px]">
                  <Pill>session {snap ? `…${snap.session.slice(-6)}` : "—"}</Pill>
                  <Pill>Exasol clock {snap ? snap.db_time.slice(11, 19) : "—"}</Pill>
                  <Pill tone={hot.exasol ? "live" : undefined}>
                    <span className={`h-1.5 w-1.5 rounded-full ${hot.exasol ? "bg-kelp" : "bg-mist/50"}`} /> synced {ago(snap?.ts)}{snap ? ` · read in ${snap.ms} ms` : ""}
                  </Pill>
                </div>
              </button>
            </div>

            {/* connectors */}
            <svg className="absolute left-0 top-0" width={W} height={H} viewBox={`0 0 ${W} ${H}`} fill="none">
              <Base rows={ctlRows} />
              {ctlRows.map((r) => { const y = rowY(ctlRows, r)!; return <path key={r} d={pathOf([[TRUNK, y], [828, y]])} stroke={hotRows[r] ?? "#d6dee9"} strokeWidth={hotRows[r] ? 2 : 1.25} />; })}
              <AnimatePresence>
                {glows.map((g) => (
                  <motion.g key={g.k} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.35 }}>
                    <path d={g.d} stroke={g.color} strokeOpacity={0.14} strokeWidth={8} strokeLinecap="round" strokeLinejoin="round" />
                    <path d={g.d} stroke={g.color} strokeOpacity={0.85} strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" />
                  </motion.g>
                ))}
              </AnimatePresence>
            </svg>

            {/* actors */}
            <NodeCard r={R.human} {...node("human")} icon={UserRound} color={C.review} title="Human reviewer" sub="you, in this browser">
              <Line>{humanMerges} merge{humanMerges === 1 ? "" : "s"} approved by you</Line>
            </NodeCard>
            <NodeCard r={R.driver} {...node("driver")} icon={agent ? Bot : ScrollText} color={agent ? C.ai : C.ctl}
              title={agent ? "Gemini planner" : "Scripted playbook"} sub={agent ? (cfg?.planner_model ?? "plans and picks tools") : "fixed SQL, no AI"}
              thinking={!!run && !run.ended && agent}>
              <Line>{run ? <>run <b className="font-semibold text-fog">{run.id}</b> · tier {run.tier} · gate {run.gateEnabled ? "on" : "off"}{run.ended ? " · ended" : " · running"}</> : "no run yet"}</Line>
            </NodeCard>
            <NodeCard r={R.gemini} {...node("gemini")} icon={Sparkles} color={C.ai}
              title={adjudicatorInfo(s.adjudicator ?? cfg?.adjudicator_rung)?.label ?? "Adjudicator"} sub="split-vote adjudicator"
              thinking={!!adj && adj.verdicts == null}>
              <Line>{cfg ? `${cfg.adjudicator_model} · ` : ""}{adj ? (adj.verdicts ? `${fmt(adj.pairs)} pairs · ${fmt(adj.verdicts.SAME_PERSON ?? 0)} same · ${fmt(adj.verdicts.DIFFERENT_PERSON ?? 0)} different` : `adjudicating ${fmt(adj.pairs)} pairs…`) : "idle"}</Line>
            </NodeCard>

            {/* interfaces */}
            <NodeCard r={R.orch} {...node("orch")} icon={Server} color={C.ctl} title="Orchestrator" sub="FastAPI · gate · live stream">
              <div className="mt-3 space-y-2">
                <MiniRow icon={ShieldCheck} label="Last gate decision" value={gateLine(s)} />
                <MiniRow icon={Layers} label={`Tier ${run?.tier ?? "—"} · TIER_CONFIG`} value={tierLine(snap, run?.tier)} />
                <MiniRow icon={Workflow} label="Branch statements" value={stmtLine(s)} />
              </div>
            </NodeCard>
            <NodeCard r={R.dmcp} {...node("dmcp")} icon={Plug} color={C.rows} title="Drydock MCP" sub="agent's only write path" dim={!agent}>
              <Line>{agent ? "active · as DRYDOCK_SVC" : "idle in scripted mode"}</Line>
            </NodeCard>
            <NodeCard r={R.xmcp} {...node("xmcp")} icon={Eye} color={C.read} title="Exasol MCP (official)" sub={cfg ? `read-only · ${cfg.mcp_row_limit}-row limit` : "read-only"} dim={!agent}>
              <Line>{agent ? "active · as DRYDOCK_AGENT" : "idle in scripted mode"}</Line>
            </NodeCard>

            {/* tables */}
            <GoldenCard r={R.golden} hot={hot.golden} onClick={() => setSel({ kind: "table", fq: "GOLDEN.CUSTOMERS" })}
              rows={rows("GOLDEN.CUSTOMERS")} fp={s.golden.fingerprint} archives={snap ? archives.length : null}
              view={(snap?.tables ?? []).some((t) => t.kind === "VIEW" && t.schema === "GOLDEN_V")} />
            <BranchesCard r={R.branches} hot={hot.branches} list={branchList} live={snap ? liveSchemas : null} rows={rows} total={s.order.length}
              onClick={() => setSel({ kind: "node", id: "branches" })} />
            <ListCard r={R.erwork} hot={hot.erwork} icon={Layers} color={C.ai} schema="ER_WORK" title="Match decisions"
              items={erTables.map((t) => ({ name: t.table, rows: t.rows }))} empty="no run published yet" max={4}
              onClick={() => setSel({ kind: "node", id: "erwork" })} />
            <ControlCard r={R.control} hot={hot.control} hotRows={hotRows} list={ctlRows} rows={(t) => rows(`DRYDOCK.${t}`)}
              onRow={(t) => setSel({ kind: "table", fq: `DRYDOCK.${t}` })} onClick={() => setSel({ kind: "node", id: "control" })} />
            <TableCard r={R.srcA} hot={hot.srcA} icon={Table2} color={C.rows} fq="SOURCE_A.CUSTOMERS" note="read-only"
              rows={rows("SOURCE_A.CUSTOMERS")} cols={cols("SOURCE_A.CUSTOMERS")} keys={keysOf(snap, "SOURCE_A.CUSTOMERS")} onClick={() => setSel({ kind: "table", fq: "SOURCE_A.CUSTOMERS" })} />
            <TableCard r={R.srcB} hot={hot.srcB} icon={Table2} color={C.ai} fq="SOURCE_B.CLIENTS" note="read-only"
              rows={rows("SOURCE_B.CLIENTS")} cols={cols("SOURCE_B.CLIENTS")} keys={keysOf(snap, "SOURCE_B.CLIENTS")} onClick={() => setSel({ kind: "table", fq: "SOURCE_B.CLIENTS" })} />
            <ListCard r={R.bench} hot={hot.bench} icon={Lock} color={C.bad} schema="BENCH" title="Answer key · sealed"
              badge="no agent access" items={(snap?.tables ?? []).filter((t) => t.schema === "BENCH").map((t) => ({ name: t.table, rows: t.rows }))}
              empty="—" onClick={() => setSel({ kind: "node", id: "bench" })} muted />

            {/* live traffic */}
            {dots.map((d) => (
              <span key={d.k} className="dd-dot" style={{ offsetPath: `path("${d.d}")`, width: d.size, height: d.size, opacity: d.op,
                "--c": d.color, "--dur": `${d.dur}s`, "--delay": `${d.delay}s` } as React.CSSProperties} />
            ))}
            <AnimatePresence>
              {labels.map((l) => (
                <motion.div key={l.k} initial={{ opacity: 0, y: 6, scale: 0.96 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: -4 }}
                  transition={{ duration: 0.3 }} className="pointer-events-none absolute z-20" style={{ left: l.x, top: l.y }}>
                  <div className="-translate-x-1/2 -translate-y-1/2 whitespace-nowrap rounded-full border bg-white/95 px-3 py-1.5 text-[12px] font-medium text-fog shadow-[0_6px_20px_-8px_rgba(15,23,42,.35)] backdrop-blur"
                    style={{ borderColor: `${l.color}40` }}>
                    <span className="mr-1.5 inline-block h-1.5 w-1.5 -translate-y-px rounded-full" style={{ background: l.color }} />{l.text}
                  </div>
                </motion.div>
              ))}
            </AnimatePresence>
          </div>
        </div>
        {sel && <Inspector sel={sel} s={s} onClose={() => setSel(null)} rows={rows} cols={cols} />}
      </div>

      {/* event log */}
      <div className="card overflow-hidden">
        <div className="flex items-center justify-between border-b border-rule px-5 py-3">
          <div className="flex items-center gap-2 text-[14px] font-semibold text-fog"><Workflow className="h-4 w-4 text-tide" /> Live event log</div>
          <div className="text-[12px] text-mist">
            {idle ? "Waiting for activity — start a run, approve a merge, unmerge, or Refresh from Exasol." : `last event ${ago(lastEvent?.ts)}`}
          </div>
        </div>
        <div className="max-h-[300px] overflow-y-auto">
          <>
            {[...s.recent].reverse().slice(0, 14).map((e) => {
              const f = flowsFor(e, s)[0];
              return (
                <button key={evKey(e)}
                  onClick={() => flowsFor(e, s).forEach((x) => setHot((h) => { const y = { ...h }; x.nodes.forEach((n) => { y[n] = { c: x.color, n: ++seq.current }; }); return y; }))}
                  className="dd-flash flex w-full items-center gap-4 border-b border-rule/70 px-5 py-2.5 text-left text-[12.5px] hover:bg-deck/60">
                  <span className="num w-[92px] shrink-0 font-mono text-[11.5px] text-mist">{tsFmt(e.ts)}</span>
                  <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: f?.color ?? C.idle }} />
                  <span className="w-[150px] shrink-0 font-mono text-[11.5px] text-fog/70">{e.type}</span>
                  <span className="truncate text-fog">{describe(e, s)}</span>
                </button>
              );
            })}
          </>
          {s.recent.length === 0 && <div className="px-5 py-6 text-[13px] text-mist">No events yet on this connection.</div>}
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ pieces

const evKey = (e: DrydockEvent) =>
  `${e.ts}|${e.type}|${e.branch_id ?? ""}|${e.payload?.op_id ?? e.payload?.merge_id ?? e.payload?.pair_id ?? e.payload?.kind ?? ""}`;

const tsFmt = (ts: string) => {
  const d = new Date(ts);
  return isNaN(+d) ? "" : `${d.toLocaleTimeString("en-GB", { hour12: false })}.${String(d.getMilliseconds()).padStart(3, "0")}`;
};

function tierLine(snap: Snapshot | null, tier?: number): string {
  const t = snap?.config.tiers.find((x) => x.tier === tier);
  if (!t) return snap ? "no run yet" : "waiting for Exasol";
  if (t.max_changed <= 0) return `${t.label} · merges only with a person`;
  return `≤${fmt(t.max_changed)} rows · risk ≤${fmt(t.max_risk)} · deletes ≤${t.max_delete_pct}%`;
}

function stmtLine(s: State): string {
  const all = Object.values(s.branches);
  const ok = all.reduce((a, b) => a + b.ops.filter((o) => o.status === "OK").length, 0);
  const bad = all.reduce((a, b) => a + b.ops.filter((o) => o.status !== "OK").length + b.blocked.length, 0);
  return `${fmt(ok)} ran · ${fmt(bad)} refused`;
}

function gateLine(s: State): string {
  const b = [...s.order].reverse().map((id) => s.branches[id]).find((x) => x?.gate);
  if (!b?.gate) return "no request yet";
  const d = b.gate.decision;
  if (d === "PENDING" && b.applied) return `held → merged by you · ${b.cls}`;
  if (d === "PENDING" && b.rejected) return `held → rejected by you · ${b.cls}`;
  if (d === "PENDING") return `held for a person · ${b.cls}`;
  return `${d === "MERGE" ? "merged within tier" : d.toLowerCase()} · ${b.cls}`;
}

function Base({ rows }: { rows: string[] }) {
  const ys = rows.map((r) => rowY(rows, r)!);
  const top = Math.min(250, ...ys), bottom = Math.max(700, ...ys);
  const solid = { stroke: "#d0d9e5", strokeWidth: 1.4, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  const dashed = { ...solid, strokeDasharray: "5 5" };
  const dotted = { ...solid, strokeDasharray: "1 6" };
  return (
    <g>
      <path d={pathOf(E.human_orch)} {...solid} />
      <path d={pathOf(E.driver_orch)} {...dotted} />
      <path d={pathOf(E.driver_dmcp)} {...solid} />
      <path d={pathOf(E.driver_xmcp)} {...dashed} />
      <path d={pathOf(E.gemini_orch)} {...solid} />
      <path d={pathOf(E.gemini_dmcp)} {...solid} />
      <path d={pathOf(E.orch_golden)} {...solid} />
      <path d={pathOf(E.orch_branches)} {...solid} />
      <path d={pathOf(E.dmcp_branches)} {...solid} />
      <path d={pathOf(JOIN.orch)} {...solid} />
      <path d={pathOf(JOIN.dmcp)} {...solid} />
      <path d={pathOf(JOIN.golden)} {...solid} />
      <path d={pathOf(JOIN.branches)} {...solid} />
      <path d={pathOf(JOIN.erwork)} {...solid} />
      <path d={pathOf([[TRUNK, top], [TRUNK, bottom]])} stroke="#c3cedc" strokeWidth={2} strokeLinecap="round" />
      <path d={pathOf(E.xmcp_golden)} {...dashed} />
      <path d={pathOf(E.xmcp_srcA)} {...dashed} />
      <path d={pathOf(E.xmcp_srcB)} {...dashed} />
      <path d={pathOf(E.golden_branches)} {...solid} />
      <path d={pathOf(E.branches_golden)} {...solid} />
      <path d={pathOf(E.erwork_branches)} {...solid} />
      <path d={pathOf(E.srcA_raw)} {...solid} />
      <path d={pathOf(E.srcB_raw)} {...solid} />
      {/* ports */}
      {[E.human_orch, E.driver_dmcp, E.orch_golden, E.dmcp_branches, E.golden_branches, E.branches_golden, E.erwork_branches, E.srcA_raw, E.srcB_raw, E.xmcp_golden]
        .flatMap((p) => [p[0], p[p.length - 1]]).map(([x, y], i) => <circle key={i} cx={x} cy={y} r={2.6} fill="#fff" stroke="#b9c5d4" strokeWidth={1.2} />)}
    </g>
  );
}

function Legend() {
  const Item = ({ c, t }: { c: string; t: string }) => (
    <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: c, boxShadow: `0 0 0 3px ${c}22` }} />{t}</span>
  );
  const LineK = ({ dash, t }: { dash?: string; t: string }) => (
    <span className="flex items-center gap-1.5"><svg width="26" height="6"><path d="M1 3 H25" stroke="#94a3b8" strokeWidth="1.6" strokeDasharray={dash} strokeLinecap="round" /></svg>{t}</span>
  );
  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-xl border border-rule bg-hull px-4 py-2.5 text-[12px] text-fog/80">
      <LineK t="write path" /><LineK dash="5 5" t="read-only" /><LineK dash="1 6" t="in-process" />
      <span className="h-4 w-px bg-rule" />
      <Item c={C.rows} t="rows moving" /><Item c={C.ai} t="matching & AI" /><Item c={C.review} t="waits for a person" />
      <Item c={C.merge} t="merged into GOLDEN" /><Item c={C.bad} t="refused / dropped" /><Item c={C.ctl} t="control & metadata" /><Item c={C.read} t="agent read" />
      <span className="h-4 w-px bg-rule" />
      <span className="flex items-center gap-1.5"><KeyRound className="h-3.5 w-3.5 text-brass" /> declared key</span>
      <span className="flex items-center gap-1.5"><KeyRound className="h-3.5 w-3.5 text-tide" /> diff key (verified unique)</span>
      <span className="ml-auto text-mist">Click any box for live details</span>
    </div>
  );
}

function ColumnLabel({ x, text }: { x: number; text: string }) {
  return <div className="absolute text-[10.5px] font-semibold uppercase tracking-[0.18em] text-mist/80" style={{ left: x, top: 34 }}>{text}</div>;
}

function IconTile({ icon: Icon, color, size = 36 }: { icon: React.ElementType; color: string; size?: number }) {
  return (
    <span className="grid shrink-0 place-items-center rounded-xl"
      style={{ width: size, height: size, background: `linear-gradient(140deg, ${color}26, ${color}0d)`, boxShadow: `inset 0 0 0 1px ${color}22` }}>
      <Icon style={{ color, width: size * 0.5, height: size * 0.5 }} strokeWidth={2} />
    </span>
  );
}

function Pill({ children, tone }: { children: React.ReactNode; tone?: "live" }) {
  return <span className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-medium ${tone === "live" ? "border-kelp/30 bg-kelp/5 text-fog" : "border-rule bg-white text-fog/80"}`}>{children}</span>;
}

function Line({ children }: { children: React.ReactNode }) {
  return <div className="mt-2.5 line-clamp-2 border-t border-rule/70 pt-2 text-[11.5px] leading-snug text-mist">{children}</div>;
}

function MiniRow({ icon: Icon, label, value }: { icon: React.ElementType; label: string; value: string }) {
  return (
    <div className="flex items-center gap-2.5 rounded-lg bg-deck/70 px-2.5 py-1.5">
      <Icon className="h-3.5 w-3.5 shrink-0 text-navy/70" />
      <div className="min-w-0 leading-tight">
        <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-mist">{label}</div>
        <div className="text-[11.5px] font-medium leading-snug text-fog">{value}</div>
      </div>
    </div>
  );
}

function Ring({ hot }: { hot?: { c: string; n: number } }) {
  return hot ? <span key={hot.n} className="dd-ring" style={{ "--c": hot.c } as React.CSSProperties} /> : null;
}

function Shell({ r, hot, selected, onClick, children, dim, className = "" }:
  { r: Rect; hot?: { c: string; n: number }; selected?: boolean; onClick?: () => void; children: React.ReactNode; dim?: boolean; className?: string }) {
  return (
    <div onClick={onClick} role="button"
      className={`absolute cursor-pointer rounded-2xl border bg-white transition-[box-shadow,border-color,opacity] duration-300 ${dim ? "opacity-60" : ""}
        ${selected ? "border-tide/50 shadow-[0_0_0_4px_rgba(37,99,235,.10)]" : "border-rule hover:border-tide/30"}
        shadow-[0_1px_2px_rgba(15,23,42,.05),0_10px_28px_-16px_rgba(15,23,42,.22)] ${className}`}
      style={{ left: r.x, top: r.y, width: r.w, height: r.h, ...(hot ? { borderColor: `${hot.c}66`, boxShadow: `0 0 0 4px ${hot.c}14, 0 10px 28px -14px ${hot.c}55` } : {}) }}>
      <Ring hot={hot} />
      {children}
    </div>
  );
}

function NodeCard({ r, hot, selected, onClick, icon, color, title, sub, children, dim, thinking }:
  { r: Rect; hot?: { c: string; n: number }; selected?: boolean; onClick?: () => void; icon: React.ElementType; color: string;
    title: string; sub: string; children?: React.ReactNode; dim?: boolean; thinking?: boolean }) {
  return (
    <Shell r={r} hot={hot} selected={selected} onClick={onClick} dim={dim} className="px-3.5 py-3">
      <div className="flex items-center gap-3">
        <IconTile icon={icon} color={color} size={32} />
        <div className="min-w-0 leading-tight">
          <div className="truncate text-[13.5px] font-semibold text-fog">{title}</div>
          <div className="truncate text-[11px] text-mist">{sub}</div>
        </div>
        {thinking && <span className="dd-think ml-auto h-2 w-2 shrink-0 rounded-full" style={{ background: color }} title="working" />}
      </div>
      {children}
    </Shell>
  );
}

function Count({ v }: { v: number | null }) {
  return v == null ? <span className="text-mist">—</span> : <CountUp value={v} />;
}

function GoldenCard({ r, hot, onClick, rows, fp, archives, view }: { r: Rect; hot?: { c: string; n: number }; onClick: () => void; rows: number | null; fp: string | null; archives: number | null; view: boolean }) {
  return (
    <Shell r={r} hot={hot} onClick={onClick} className="px-4 py-3.5">
      <div className="flex items-center gap-3">
        <IconTile icon={Gem} color="#b45309" />
        <div className="leading-tight">
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.14em] text-mist">GOLDEN</div>
          <div className="font-mono text-[13px] font-semibold text-fog">CUSTOMERS</div>
        </div>
      </div>
      <div className="mt-3 flex items-baseline gap-2">
        <span className="text-[28px] font-bold leading-none tracking-tight text-fog"><Count v={rows} /></span><span className="text-[12px] text-mist">rows</span>
        <span className="ml-auto rounded-md bg-[#b45309]/10 px-1.5 py-0.5 text-[10.5px] font-semibold text-[#b45309]">production</span>
      </div>
      <div className="mt-1 font-mono text-[11px] text-mist" title={fp ?? ""}>fingerprint {short(fp)}</div>
      <div className="mt-2.5 flex flex-wrap gap-1.5 text-[10.5px]">
        {view && <span className="rounded-md border px-1.5 py-0.5 text-[#0d9488]" style={{ borderColor: "#0d948840" }} title="GOLDEN_V.CUSTOMERS exists in Exasol">agent reads via GOLDEN_V</span>}
        {archives != null && <span className="flex items-center gap-1 rounded-md border border-rule px-1.5 py-0.5 text-mist" title="GOLDEN.CUSTOMERS__ARCH_* tables in Exasol — each merge keeps one for unmerge"><Archive className="h-3 w-3" />{archives} archive{archives === 1 ? "" : "s"}</span>}
      </div>
    </Shell>
  );
}

function BranchesCard({ r, hot, list, live, rows, total, onClick }:
  { r: Rect; hot?: { c: string; n: number }; list: Branch[]; live: Set<string> | null; rows: (fq: string) => number | null; total: number; onClick: () => void }) {
  const tone = (st: string) => st === "MERGED" ? "text-kelp bg-kelp/10" : st === "PENDING" ? "text-brass bg-brass/10" : st === "OPEN" ? "text-tide bg-tide/10"
    : st === "UNMERGED" ? "text-sky bg-sky/10" : "text-mist bg-deck";
  return (
    <Shell r={r} hot={hot} onClick={onClick} className="px-4 py-3.5">
      <div className="flex items-center gap-3">
        <IconTile icon={GitBranch} color={C.rows} />
        <div className="leading-tight">
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.14em] text-mist">BR_*</div>
          <div className="text-[13px] font-semibold text-fog">Branches</div>
        </div>
        {live && <span className="ml-auto whitespace-nowrap rounded-md bg-kelp/10 px-1.5 py-0.5 text-[10.5px] font-semibold text-kelp" title="BR_* schemas that exist in Exasol right now">{live.size} live in Exasol</span>}
      </div>
      <div className="mt-3 space-y-1.5">
          {list.slice(0, 3).map((b) => {
            const exists = live?.has(b.id);
            const n = exists ? rows(`${b.id}.CUSTOMERS`) : b.materialised?.rows ?? null;
            return (
              <div key={b.id} className="dd-flash rounded-lg border border-rule/80 bg-deck/40 px-2.5 py-1">
                <div className="flex items-center gap-2 text-[11.5px]">
                  <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${live == null ? "bg-mist/30" : exists ? "bg-kelp" : "bg-mist/40"}`}
                    title={live == null ? "waiting for Exasol" : exists ? "schema exists in Exasol now" : "schema no longer in Exasol"} />
                  <span className="min-w-0 flex-1 truncate font-medium text-fog" title={b.cls}>{b.cls || b.id}</span>
                  <span className="num shrink-0 text-[10.5px] text-mist" title={exists ? "row count Exasol reports now" : "row count when the branch was copied; the schema is gone now"}>{n != null ? fmt(n) : ""}</span>
                </div>
                <div className="flex items-center gap-2 whitespace-nowrap pl-3.5 text-[10.5px] text-mist">
                  <span className="font-mono">{b.id}</span>
                  <span className={`ml-auto shrink-0 rounded px-1.5 py-px text-[9.5px] font-semibold ${tone(b.status)}`}>{b.status}</span>
                </div>
              </div>
            );
          })}
          {total > 3 && <div className="pl-1 text-[11px] text-mist">+{total - 3} earlier branch{total - 3 === 1 ? "" : "es"} · click for all</div>}
        {list.length === 0 && <div className="text-[12px] text-mist">No branch yet.</div>}
      </div>
    </Shell>
  );
}

function ListCard({ r, hot, icon, color, schema, title, items, empty, onClick, badge, muted, max = 5 }:
  { r: Rect; hot?: { c: string; n: number }; icon: React.ElementType; color: string; schema: string; title: string;
    items: { name: string; rows: number | null }[]; empty: string; onClick: () => void; badge?: string; muted?: boolean; max?: number }) {
  return (
    <Shell r={r} hot={hot} onClick={onClick} className="px-4 py-3.5">
      <div className="flex items-center gap-3">
        <IconTile icon={icon} color={color} size={32} />
        <div className="min-w-0 leading-tight">
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.14em] text-mist">{schema}</div>
          <div className="truncate text-[13px] font-semibold text-fog">{title}</div>
        </div>
      </div>
      {badge && <div className="mt-2 inline-flex items-center gap-1 rounded-md bg-flare/5 px-1.5 py-0.5 text-[10.5px] font-medium text-flare"><Lock className="h-3 w-3" />{badge}</div>}
      <div className={`mt-2 space-y-0.5 ${muted ? "opacity-70" : ""}`}>
        {items.slice(0, max).map((t) => (
          <div key={t.name} className="flex items-center gap-2 text-[11px] leading-[18px]">
            <Table2 className="h-3 w-3 shrink-0 text-mist" /><span className="truncate font-mono text-fog/85">{t.name}</span>
            <span className="num ml-auto text-mist"><Count v={t.rows} /></span>
          </div>
        ))}
        {items.length > max && <div className="text-[10.5px] text-mist">+{items.length - max} more · click for all</div>}
        {items.length === 0 && <div className="text-[12px] text-mist">{empty}</div>}
      </div>
    </Shell>
  );
}

function ControlCard({ r, hot, hotRows, list, rows, onRow, onClick }:
  { r: Rect; hot?: { c: string; n: number }; hotRows: Record<string, string>; list: string[]; rows: (t: string) => number | null; onRow: (t: string) => void; onClick: () => void }) {
  const h = rowH(list.length);
  return (
    <Shell r={r} hot={hot} className="px-0 py-0">
      <div onClick={onClick} className="flex items-center gap-3 px-4 pb-2 pt-3.5">
        <IconTile icon={Settings2} color={C.ctl} size={32} />
        <div className="leading-tight">
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.14em] text-mist">DRYDOCK</div>
          <div className="text-[13px] font-semibold text-fog">Control plane</div>
        </div>
      </div>
      <div className="absolute inset-x-0" style={{ top: ROW_TOP - r.y }}>
        {list.length === 0 && <div className="px-4 py-2 text-[12px] text-mist">Waiting for Exasol to report the DRYDOCK tables…</div>}
        {list.map((t) => {
          const c = hotRows[t];
          return (
            <div key={t} onClick={(e) => { e.stopPropagation(); onRow(t); }}
              className="relative flex items-center gap-2 px-4 text-[11.5px] transition-colors duration-300 hover:bg-deck/70"
              style={{ height: h, ...(c ? { background: `${c}12` } : {}) }}>
              <span className="h-1.5 w-1.5 shrink-0 rounded-full transition-colors" style={{ background: c ?? "#cbd5e1" }} />
              <span className="truncate font-mono text-fog/90">{t}</span>
              <span className="num ml-auto font-medium text-fog/80"><Count v={rows(t)} /></span>
            </div>
          );
        })}
      </div>
    </Shell>
  );
}

function TableCard({ r, hot, icon, color, fq, note, rows, cols, keys, onClick }:
  { r: Rect; hot?: { c: string; n: number }; icon: React.ElementType; color: string; fq: string; note: string; rows: number | null;
    cols: [string, string][]; keys: Record<string, string>; onClick: () => void }) {
  const [schema, table] = fq.split(".");
  const ordered = [...cols].sort((a, b) => (keys[b[0]] ? 1 : 0) - (keys[a[0]] ? 1 : 0));
  return (
    <Shell r={r} hot={hot} onClick={onClick} className="px-4 py-3.5">
      <div className="flex items-center gap-3">
        <IconTile icon={icon} color={color} size={32} />
        <div className="min-w-0 leading-tight">
          <div className="text-[10.5px] font-semibold uppercase tracking-[0.14em] text-mist">{schema}</div>
          <div className="truncate font-mono text-[13px] font-semibold text-fog">{table}</div>
        </div>
      </div>
      <div className="mt-2 flex items-baseline gap-1.5"><span className="text-[20px] font-bold tracking-tight text-fog"><Count v={rows} /></span><span className="text-[11px] text-mist">rows</span>
        <span className="ml-auto rounded-md bg-deck px-1.5 py-0.5 text-[10.5px] font-medium text-mist">{note}</span></div>
      <div className="mt-1.5 space-y-0.5">
        {ordered.slice(0, 4).map(([n, t]) => <ColRow key={n} name={n} type={t} k={keys[n]} />)}
        {cols.length > 4 && <div className="pt-0.5 text-[10.5px] text-mist">+{cols.length - 4} more columns</div>}
      </div>
    </Shell>
  );
}

function ColRow({ name, type, k }: { name: string; type: string; k?: string }) {
  return (
    <div className="flex items-center gap-1.5 text-[11px]">
      {k?.startsWith("PRIMARY KEY") ? <KeyRound className="h-3 w-3 shrink-0 text-brass" /> : k?.startsWith("DIFF KEY") ? <KeyRound className="h-3 w-3 shrink-0 text-tide" />
        : k ? <Link2 className="h-3 w-3 shrink-0 text-tide" /> : <span className="grid h-3 w-3 shrink-0 place-items-center"><span className="h-1 w-1 rounded-full bg-mist/50" /></span>}
      <span className="truncate font-mono text-fog/90" title={k ? `${k} (from Exasol)` : undefined}>{name}</span>
      <span className="ml-auto shrink-0 font-mono text-[10px] text-mist">{type.replace(" UTF8", "").toLowerCase()}</span>
    </div>
  );
}

// ------------------------------------------------------------------ inspector

const ABOUT: Record<string, { title: string; icon: React.ElementType; color: string; text: string }> = {
  human: { title: "Human reviewer", icon: UserRound, color: C.review, text: "You, in this browser. Approve, reject, select rows and unmerge through the orchestrator's REST API — the only human verbs. The agent has no access to them." },
  driver: { title: "Run driver", icon: Bot, color: C.ai, text: "Scripted mode runs fixed SQL in-process — no AI planning. Agent mode is Gemini planning and choosing tools, reaching Exasol only through the two MCP servers." },
  gemini: { title: "Gemini adjudicator", icon: Sparkles, color: C.ai, text: "Asked only about pairs the three SQL matchers disagreed on. It receives a comparison summary — never names, emails, phones, addresses or dates — and its verdict is advice: split rows still wait for a person." },
  orch: { title: "Orchestrator", icon: Server, color: C.ctl, text: "FastAPI. Runs scripted plays in-process, hosts the merge gate, the reviewer REST actions and this live stream. Every statement it sends passes the dialect firewall in Db.execute before Exasol sees it." },
  dmcp: { title: "Drydock MCP server", icon: Plug, color: C.rows, text: "The agent's only write path, running as DRYDOCK_SVC: open a branch, run any SQL (retargeted into the branch), diff, request a merge, discard. It never writes GOLDEN directly." },
  xmcp: { title: "Exasol MCP server (official)", icon: Eye, color: C.read, text: "exasol-mcp-server as DRYDOCK_AGENT: queries on, writes off, a 50-row limit, and only SOURCE_A, SOURCE_B and the GOLDEN_V view are visible. Rows it returns to the planner are counted and disclosed." },
  branches: { title: "Branches", icon: GitBranch, color: C.rows, text: "One schema per match class. CREATE TABLE … AS SELECT copies GOLDEN; the agent's SQL runs here; DROP SCHEMA … CASCADE is the whole undo for unmerged work." },
  control: { title: "DRYDOCK control plane", icon: Settings2, color: C.ctl, text: "Drydock's own bookkeeping in Exasol: runs, candidate pairs, votes, adjudications, precedents, branches, every statement, row-level diffs, merges and this event log." },
  erwork: { title: "ER_WORK", icon: Layers, color: C.ai, text: "Each run's published match decisions (normalised sources, matches, new customers, duplicates). Branch SQL reads them; branches cannot write them." },
  bench: { title: "BENCH — answer key", icon: Lock, color: C.bad, text: "Ground truth for scoring after the fact. The agent holds no grant on it, it is never in a prompt, and the scorer is a human-only endpoint." },
  exasol: { title: "Exasol", icon: Database, color: C.ctl, text: "Every table lives here and almost all the work runs here: copies, row hashing, the diff, the matching and the swap. These numbers are re-read from the instance about a second after anything happens." },
};

function Inspector({ sel, s, onClose, rows, cols }:
  { sel: NonNullable<Sel>; s: State; onClose: () => void; rows: (fq: string) => number | null; cols: (fq: string) => [string, string][] }) {
  const snap = s.system.snapshot;
  const fq = sel.kind === "table" ? sel.fq : null;
  const nodeId = sel.kind === "node" ? sel.id : fq?.startsWith("DRYDOCK.") ? "control" : fq?.startsWith("GOLDEN") ? "golden"
    : fq?.startsWith("SOURCE_A") ? "srcA" : fq?.startsWith("SOURCE_B") ? "srcB" : "exasol";
  const tableText = !fq ? null
    : fq.startsWith("GOLDEN") ? "Production. Written only by drydock/merge.py: stage a copy, check its fingerprint against the expected one, then swap it in with two RENAMEs in one transaction. The agent reads it only through the GOLDEN_V view."
    : fq.startsWith("SOURCE") ? "An input system, owned by the admin and read-only to everyone. The agent reads it through the official Exasol MCP server."
    : fq.startsWith("DRYDOCK.") ? ABOUT.control.text : ABOUT.exasol.text;
  const about = fq ? { title: fq, icon: fq.startsWith("GOLDEN") ? Gem : fq.startsWith("DRYDOCK.") ? Settings2 : Table2,
    color: fq.startsWith("GOLDEN") ? "#b45309" : fq.startsWith("DRYDOCK.") ? C.ctl : C.rows, text: tableText! } : ABOUT[nodeId] ?? ABOUT.exasol;
  const Icon = about.icon;
  const activity = [...s.recent].reverse().filter((e) => flowsFor(e, s).some((f) => f.nodes.includes(nodeId)
    || (fq?.startsWith("DRYDOCK.") && f.rows?.includes(fq.split(".")[1])))).slice(0, 8);
  const keys = fq ? keysOf(snap, fq) : {};
  const tableCols = fq ? cols(fq) : [];
  return (
    <motion.aside initial={{ x: 24, opacity: 0 }} animate={{ x: 0, opacity: 1 }} transition={{ duration: 0.25 }}
      className="absolute bottom-3 right-3 top-3 z-30 flex w-[370px] flex-col overflow-hidden rounded-2xl border border-rule bg-white/97 shadow-[0_24px_60px_-20px_rgba(15,23,42,.35)] backdrop-blur">
      <div className="flex items-center gap-3 border-b border-rule px-4 py-3.5">
        <IconTile icon={Icon} color={about.color} size={34} />
        <div className="min-w-0 leading-tight">
          <div className="truncate font-semibold text-fog">{fq ?? about.title}</div>
          <div className="text-[11.5px] text-mist">{fq ? (rows(fq) != null ? `${fmt(rows(fq))} rows · ${tableCols.length} columns` : "view") : "live details"}</div>
        </div>
        <button onClick={onClose} className="ml-auto text-mist hover:text-fog"><X className="h-5 w-5" /></button>
      </div>
      <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4 text-[12.5px]">
        <p className="leading-relaxed text-fog/85">{about.text}</p>
        {nodeId === "exasol" && snap && (
          <dl className="grid grid-cols-2 gap-2">
            {[["version", snap.version], ["session", snap.session], ["DB clock", snap.db_time.slice(0, 23)], ["tables", String(snap.tables.length)],
              ["last read", `${snap.ms} ms`], ["trigger", snap.trigger]].map(([a, b]) => (
              <div key={a} className="rounded-lg bg-deck/70 px-2.5 py-2"><dt className="text-[10.5px] uppercase tracking-wide text-mist">{a}</dt><dd className="truncate font-mono text-[11.5px] text-fog" title={b}>{b}</dd></div>
            ))}
          </dl>
        )}
        {nodeId === "exasol" && snap && (
          <div>
            <div className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.14em] text-mist">Gate tiers (DRYDOCK.TIER_CONFIG)</div>
            <div className="divide-y divide-rule/70 rounded-xl border border-rule text-[11.5px]">
              {snap.config.tiers.map((t) => (
                <div key={t.tier} className="flex items-center gap-2 px-3 py-1.5">
                  <span className="font-semibold text-fog">{t.tier}</span><span className="text-fog/80">{t.label}</span>
                  <span className="ml-auto font-mono text-[10.5px] text-mist">{t.max_changed > 0 ? `≤${fmt(t.max_changed)} rows · risk ≤${fmt(t.max_risk)} · del ≤${t.max_delete_pct}%` : "no automatic merge"}</span>
                </div>
              ))}
            </div>
            <div className="mt-2 text-[11px] text-mist">Models: planner {snap.config.planner_model} · adjudicator {snap.config.adjudicator_model} ({snap.config.adjudicator_rung})</div>
          </div>
        )}
        {tableCols.length > 0 && (
          <div>
            <div className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.14em] text-mist">Columns (live catalogue)</div>
            <div className="divide-y divide-rule/70 rounded-xl border border-rule">
              {tableCols.map(([n, t]) => (
                <div key={n} className="px-3 py-1.5">
                  <ColRow name={n} type={t} k={keys[n]} />
                  {keys[n] && <div className="pl-[18px] text-[10.5px] text-tide">{keys[n].toLowerCase()}</div>}
                </div>
              ))}
            </div>
            <div className="mt-1.5 text-[11px] text-mist">
              {Object.keys(keys).length ? "Keys are exactly those Exasol reports." : "Exasol reports no declared key on this table."} No foreign keys are declared
              in this database; how tables relate is shown by the connectors, which are Drydock's actual code paths.
            </div>
          </div>
        )}
        {sel.kind === "node" && ["erwork", "bench", "control"].includes(nodeId) && snap && (
          <div>
            <div className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.14em] text-mist">Tables Exasol reports</div>
            <div className="divide-y divide-rule/70 rounded-xl border border-rule">
              {snap.tables.filter((t) => t.schema === { erwork: "ER_WORK", bench: "BENCH", control: "DRYDOCK" }[nodeId]).map((t) => (
                <div key={t.table} className="flex items-center gap-2 px-3 py-1.5 text-[11.5px]">
                  <Table2 className="h-3 w-3 text-mist" /><span className="font-mono text-fog/90">{t.table}</span>
                  <span className="num ml-auto text-mist">{fmt(t.rows)}</span>
                </div>
              ))}
            </div>
          </div>
        )}
        {nodeId === "branches" && (
          <div className="space-y-2">
            {[...s.order].reverse().map((id) => s.branches[id]).filter(Boolean).map((b) => (
              <div key={b.id} className="rounded-xl border border-rule px-3 py-2">
                <div className="flex items-center gap-2"><span className="font-mono text-[11.5px] text-fog">{b.id}</span><span className="text-mist">{b.cls}</span><span className="ml-auto text-[11px] font-semibold text-fog/70">{b.status}</span></div>
                {b.ops.slice(-2).map((o) => <pre key={o.op_id} className="mt-1.5 max-h-24 overflow-auto whitespace-pre-wrap rounded-lg bg-deck/70 px-2 py-1.5 font-mono text-[10.5px] text-fog/80">{o.sql}</pre>)}
              </div>
            ))}
          </div>
        )}
        <div>
          <div className="mb-1.5 text-[10.5px] font-semibold uppercase tracking-[0.14em] text-mist">Recent activity</div>
          {activity.length === 0 ? <div className="text-mist">Nothing on this connection yet.</div> : (
            <div className="space-y-1.5">
              {activity.map((e) => (
                <div key={evKey(e)} className="rounded-lg bg-deck/60 px-2.5 py-1.5">
                  <div className="flex items-center gap-2 text-[10.5px] text-mist"><span className="font-mono">{tsFmt(e.ts)}</span><span className="font-mono">{e.type}</span></div>
                  <div className="text-[12px] text-fog">{describe(e, s)}</div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </motion.aside>
  );
}
