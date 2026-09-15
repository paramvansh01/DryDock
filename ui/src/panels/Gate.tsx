import { motion } from "framer-motion";
import { useState } from "react";
import { Download } from "lucide-react";
import { REJECT_CODES, api, downloads } from "../api";
import type { Branch } from "../types";
import { explainReason } from "../derive";
import { Chip, fmt, reportError, toast } from "../ui";

// The gate as a bar. The x-axis is ROWS. The threshold line is how many changed rows
// THIS confidence can buy: min(MAX_CHANGED, MAX_RISK / ((1 - confidence) · kind_weight)).
// It is computed from the limits the gate actually applied (merge.gated.limits), not configured here.
function allowedRows(b: Branch): number | null {
  const g = b.gate, d = b.diff;
  if (!g || !d) return null;
  const { max_changed = 0, max_risk = 0, confidence = 0 } = g.limits;
  const w = d.deleted > 0 ? 1 : d.changed > 0 ? 0.7 : d.added > 0 ? 0.2 : 0;
  const byRisk = w === 0 || confidence >= 1 ? Infinity : max_risk / ((1 - confidence) * w);
  return Math.min(max_changed, byRisk);
}

export function Gate({ branch, canAct }: { branch: Branch | null; canAct: boolean }) {
  const [rejectOpen, setRejectOpen] = useState(false);
  const [code, setCode] = useState(REJECT_CODES[0]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const g = branch?.gate;
  const total = g?.limits.total_changed ?? branch?.diff?.rows_total ?? 0;
  const allowed = branch ? allowedRows(branch) : null;
  const domain = Math.max(total, Math.min(allowed ?? total, total * 3), 1) * 1.15;
  const pct = (n: number) => Math.min(100, (n / domain) * 100);
  // Declarative keyframes: fill -> (bounce off the line | stop dead | run through).
  const bounce = g?.decision === "PENDING" && allowed != null && total > allowed;
  const barFrames = !g ? [0] : g.decision === "BLOCKED" ? [0, 22] : bounce
    ? [0, pct(allowed!), pct(allowed!) * 0.8, pct(allowed!) * 0.9] : [0, pct(total)];
  const lineFrames = !g || allowed == null ? [100] : [pct(g.limits.max_changed ?? total), pct(allowed)];
  if (!branch || !g) {
    return <div className="rounded-lg border border-rule bg-hull px-4 py-3 text-sm text-mist">Merge gate — waiting for a merge request.</div>;
  }
  const approvedCount = branch.approvedCount ?? (branch.diff ? branch.diff.rows_total - Object.values(branch.approvals).filter((v) => !v).length : total);
  const deselected = total - approvedCount;
  const undone = branch.status === "UNMERGED";
  const tone = undone ? "bg-mist" : branch.status === "MERGED" || g.decision === "MERGE" ? "bg-kelp" : g.decision === "PENDING" ? "bg-brass" : "bg-flare";
  const act = async (f: () => Promise<unknown>) => {
    setBusy(true);
    try { await f(); } catch (e) { reportError(e); }
    setBusy(false);
  };
  const pending = branch.status === "PENDING";
  return (
    <div className="rounded-lg border border-rule bg-hull px-4 py-2">
      <div className="mb-1.5 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-mist">Merge gate</span>
        <span className="num text-fog">{fmt(total)} changed</span>
        {deselected > 0 && <span className="num text-brass">{fmt(deselected)} deselected</span>}
        <span className="num">conf {(g.limits.confidence ?? 0).toFixed(2)}</span>
        <span className="num">risk {g.risk.toFixed(1)}</span>
        <span className="num text-mist">tier {g.tier} (max risk {g.limits.max_risk}, max rows {fmt(g.limits.max_changed)})</span>
        {pending && g.rows_needing_review > 0 && <Chip tone="brass">{fmt(g.rows_needing_review)} need review</Chip>}
        <span className="ml-auto"><Chip tone={undone ? "mist" : branch.status === "MERGED" || g.decision === "MERGE" ? "kelp" : g.decision === "PENDING" ? "brass" : "flare"}>
          {undone ? "MERGED, THEN UNDONE" : branch.status === "MERGED" ? "MERGED" : g.decision}</Chip></span>
      </div>
      <div className="relative h-5 overflow-visible rounded bg-deck">
        {/* The resting state is static (never depends on an animation finishing); the fill-and-bounce
            plays as an overlay on top of it. */}
        <div className={`absolute inset-y-0 left-0 rounded opacity-60 ${tone}`} style={{ width: `${barFrames[barFrames.length - 1]}%` }} />
        <motion.div key={`bar-${g.merge_id}-${g.decision}`} className={`absolute inset-y-0 left-0 rounded ${tone}`}
          initial={{ width: "0%" }} animate={{ width: barFrames.map((v) => `${v}%`) }}
          transition={{ duration: bounce ? 1.6 : 1.0, delay: 0.7, times: bounce ? [0, 0.55, 0.8, 1] : undefined, ease: "easeInOut" }} />
        {allowed != null && g.decision !== "BLOCKED" && (
          <>
            {/* static line at the real position; the slide from MAX_CHANGED is an overlay */}
            <div className="absolute -top-1.5 -bottom-1.5 w-0.5 bg-fog" style={{ left: `${lineFrames[lineFrames.length - 1]}%` }}
              title={`rows this confidence can buy: ${fmt(Math.round(allowed))}`} />
            <motion.div key={`line-${g.merge_id}`} className="absolute -top-1.5 -bottom-1.5 w-0.5 bg-fog/40"
              initial={{ left: `${lineFrames[0]}%`, opacity: 1 }} animate={{ left: lineFrames.map((v) => `${v}%`), opacity: 0 }}
              transition={{ duration: 0.9 }} />
          </>
        )}
      </div>
      <div className="mt-1 text-xs text-fog"><span className="font-mono text-mist">{g.reason}</span>
        {explainReason(g.reason) !== g.reason && <span className="ml-1.5">{explainReason(g.reason)}</span>}{branch.selfcheck?.discrepancy ? <span className="ml-2 text-brass">· agent lowered its own confidence</span> : null}</div>
      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        {pending && (
          <>
            <button disabled={!canAct || busy} data-tour="merge-button" onClick={() => act(async () => {
              const out = await api.approve(g.merge_id);
              if (out.decision === "BLOCKED") toast("warn", "Not merged", explainReason(out.block_reason));
              else toast("good", `Merged ${fmt(out.rows_applied)} rows into GOLDEN`, "Staged, fingerprint-checked and swapped in. You can undo it from the Diff Viewer.");
            })}
              className="rounded bg-tide px-3 py-1.5 text-sm font-semibold text-ink disabled:opacity-40">
              MERGE {fmt(approvedCount)} OF {fmt(total)}
            </button>
            <button disabled={!canAct || busy} onClick={() => setRejectOpen((v) => !v)}
              className="rounded border border-flare/60 px-3 py-1.5 text-sm text-flare disabled:opacity-40">REJECT</button>
          </>
        )}
        {(branch.status === "OPEN" || pending || branch.status === "BLOCKED") && (
          <button disabled={!canAct || busy} onClick={() => act(() => api.discard(branch.id, "discarded by reviewer"))}
            className="rounded border border-rule px-3 py-1.5 text-sm text-mist disabled:opacity-40">DISCARD</button>
        )}
        {branch.applied && (
          <span className="text-sm text-kelp">
            merged {fmt(branch.applied.rows_applied)} of {fmt(branch.applied.rows_applied + branch.applied.rows_excluded)} ·
            stage {Math.round(branch.applied.stage_ms)} ms · swap {Math.round(branch.applied.swap_ms)} ms
          </span>
        )}
        {branch.diff && (
          <a href={downloads.changes(branch.id)} data-tour="download-changes"
            title="Every row this change touches, whether it is included, and the values before and after: for sign-off before you approve"
            className="ml-auto flex items-center gap-1.5 rounded border border-rule px-3 py-1.5 text-sm text-fog hover:border-tide hover:text-tide">
            <Download className="h-3.5 w-3.5" /> Download changes (CSV)
          </a>
        )}
        {!canAct && <span className="text-xs text-mist">read-only (replay)</span>}
      </div>
      {rejectOpen && pending && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <select value={code} onChange={(e) => setCode(e.target.value)} className="rounded border border-rule bg-deck px-2 py-1 text-sm">
            {REJECT_CODES.map((c) => <option key={c}>{c}</option>)}
          </select>
          <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="note (becomes case law)"
            className="min-w-64 flex-1 rounded border border-rule bg-deck px-2 py-1 text-sm" />
          <button disabled={busy} onClick={() => act(() => api.reject(g.merge_id, code, note))}
            className="rounded bg-flare px-3 py-1 text-sm font-semibold text-ink">confirm reject</button>
        </div>
      )}
    </div>
  );
}
