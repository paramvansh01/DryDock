import { AnimatePresence, motion } from "framer-motion";
import { Scale, TriangleAlert } from "lucide-react";
import { useMemo, useState } from "react";
import { api } from "../api";
import type { Branch, Card, Pair, Rec } from "../types";
import { Chip, CountUp, Panel, PanelVotes, Verdict, fmt } from "../ui";

// Field alignment for the pair card is DERIVED from the mapping the agent declared
// (mapping.declared): each golden field's expression is scanned for the SOURCE_B columns
// it reads. Nothing about SOURCE_B's column names is hardcoded here.
const FIELDS = ["FULL_NAME", "EMAIL", "PHONE", "ADDR_LINE", "CITY", "POSTCODE", "DATE_OF_BIRTH", "COUNTRY"];
const LABEL: Record<string, string> = { FULL_NAME: "name", EMAIL: "email", PHONE: "phone", ADDR_LINE: "street",
                                        CITY: "city", POSTCODE: "postcode", DATE_OF_BIRTH: "born", COUNTRY: "country" };

function bColumns(expr: string | undefined, b: Rec | null): string[] {
  if (!expr || !b) return [];
  const words = expr.replace(/'[^']*'/g, " ").match(/[A-Z_][A-Z0-9_]*/g) ?? [];
  const seen: string[] = [];
  for (const w of words) if (w in b && !seen.includes(w)) seen.push(w);
  return seen;
}

function bValue(field: string, mapping: Record<string, string> | undefined, b: Rec | null): string | null {
  const cols = bColumns(mapping?.[field], b);
  if (!cols.length || !b) return null;
  const vals = cols.map((c) => b[c]).filter((v) => v != null && v !== "");
  return vals.length ? vals.join(" ") : null;
}

function norm(field: string, v: unknown): string {
  if (v == null) return "";
  let s = String(v).trim().toLowerCase();
  if (field === "PHONE") return s.replace(/\D/g, "").slice(-10);
  if (field === "DATE_OF_BIRTH") {
    const m = s.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
    if (m) s = `${m[3]}-${m[2]}-${m[1]}`;
    return s.slice(0, 10);
  }
  if (field === "POSTCODE" || field === "ADDR_LINE") return s.replace(/[^a-z0-9]/g, "");
  return s.replace(/\s+/g, " ");
}

function aValue(field: string, a: Rec | null): string | null {
  const v = a?.[field];
  return v == null ? null : String(v);
}

function Signals({ sig }: { sig: Pair["signals"] }) {
  const keys = ["email", "phone", "name", "addr", "dob"];
  const flags = [["suffix_conflict", "Sr/Jr conflict"], ["business_conflict", "business vs person"], ["given_initial_conflict", "given-name conflict"]]
    .filter(([k]) => sig[k]);
  return (
    <div className="flex flex-wrap items-end gap-3">
      {keys.map((k) => {
        const v = sig[k];
        return (
          <div key={k} className="w-12">
            <div className="h-8 w-full overflow-hidden rounded-sm bg-deck">
              {v != null && <div className="w-full bg-tide/70" style={{ height: `${(v / 1000) * 100}%`, marginTop: `${(1 - v / 1000) * 100}%` }} />}
            </div>
            <div className="mt-0.5 text-center text-[10px] text-mist">{k}{v == null ? " ·∅" : ""}</div>
          </div>
        );
      })}
      {sig.dob_gap_years ? <Chip tone="flare">DOB {sig.dob_gap_years}y apart</Chip> : null}
      {flags.map(([k, l]) => <Chip key={k} tone="flare">{l}</Chip>)}
    </div>
  );
}

function PairCard({ card, pair, mapping }: { card: Card; pair: Pair; mapping?: Record<string, string> }) {
  const aName = pair.kind === "AA" ? "SOURCE_A (dup)" : "SOURCE_B";
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-[72px_1fr_1fr] gap-x-3 gap-y-0.5 text-[12.5px]">
        <div />
        <div className="text-[10px] uppercase tracking-wider text-mist">SOURCE_A · {pair.a_id}</div>
        <div className="text-[10px] uppercase tracking-wider text-mist">{aName} · {pair.b_id}</div>
        {FIELDS.map((f) => {
          const av = aValue(f, pair.a);
          const bv = pair.kind === "AA" ? aValue(f, pair.b) : bValue(f, mapping, pair.b);
          if (av == null && bv == null) return null;
          const same = av != null && bv != null && norm(f, av) === norm(f, bv);
          const cls = av == null || bv == null ? "text-mist" : same ? "text-kelp" : "text-brass";
          return (
            <FieldRow key={f} label={LABEL[f]} a={av} b={bv} cls={cls} />
          );
        })}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <Signals sig={pair.signals} />
        <div className="flex items-center gap-2">
          <PanelVotes votes={pair.votes} split={pair.panel === "SPLIT"} />
          {pair.ai_verdict && (
            <Verdict verdict={pair.ai_verdict} score={pair.ai_score} adjudicator={pair.adjudicator} rationale={pair.rationale} />
          )}
          {pair.component_size > 2 && <Chip tone="brass">cluster of {pair.component_size}</Chip>}
        </div>
      </div>
      {pair.precedents.map((pr) => (
        <div key={pr.id} className="rounded border border-sky/30 bg-sky/5 px-2 py-1 text-[12px] text-fog">
          <Scale className="mr-1 inline h-3.5 w-3.5 -translate-y-px text-sky" />precedent #{pr.id} <span className={pr.verdict === "REJECTED" ? "text-flare" : "text-kelp"}>{pr.verdict}</span>
          {pr.note && <> — “{pr.note}”</>}
          <span className="text-mist"> {pr.by ? `— ${pr.by}` : ""}</span>
        </div>
      ))}
      <p className="text-[11px] text-mist">{pair.summary}</p>
    </div>
  );
}

function FieldRow({ label, a, b, cls }: { label: string; a: string | null; b: string | null; cls: string }) {
  return (
    <>
      <div className="text-[11px] text-mist">{label}</div>
      <div className={`truncate ${cls}`}>{a ?? "∅"}</div>
      <div className={`truncate ${cls}`}>{b ?? "∅"}</div>
    </>
  );
}

function RowCard({ card, branch, mapping, canAct }: { card: Card; branch: Branch; mapping?: Record<string, string>; canAct: boolean }) {
  const approved = branch.approvals[card.key] ?? card.approved;
  const [busy, setBusy] = useState(false);
  const toggle = async () => {
    if (!canAct) return;
    setBusy(true);
    const keys = card.pair?.kind === "AA"
      ? branch.diff!.rows.filter((r) => r.pair?.pair_id === card.pair!.pair_id).map((r) => r.key)
      : [card.key];
    try { await api.setRows(branch.id, keys, !approved); } catch (e) { alert(String(e)); }
    setBusy(false);
  };
  const classTone = card.class === "ADDED" ? "sky" : card.class === "DELETED" ? "flare" : "brass";
  return (
    <motion.article initial={{ y: 6 }} animate={{ y: 0 }} transition={{ duration: 0.25 }}
      className={`rounded-md border px-3 py-2.5 ${approved ? "border-rule bg-deck/40" : "border-brass/50 bg-brass/5"}`}>
      <header className="mb-2 flex items-center gap-2">
        <span className="font-mono text-xs text-fog">{card.key}</span>
        <Chip tone={classTone}>{card.class}</Chip>
        {card.pair?.panel === "SPLIT" && <Chip tone="brass">split vote</Chip>}
        {card.default_reason === "UNEXPLAINED" && <Chip tone="flare">not in any published decision</Chip>}
        <label className={`ml-auto flex items-center gap-1.5 text-xs ${canAct ? "cursor-pointer" : "cursor-not-allowed opacity-60"}`}
               title={canAct ? "include this row in the merge" : "replay / not pending: read-only"}>
          <input type="checkbox" className="h-4 w-4 accent-[var(--color-tide)]" checked={approved} disabled={!canAct || busy} onChange={toggle} />
          merge
        </label>
      </header>
      {card.pair ? (
        <PairCard card={card} pair={card.pair} mapping={mapping} />
      ) : (
        <div className="grid grid-cols-[120px_1fr_1fr] gap-x-3 text-[12.5px]">
          {(card.cols_changed.length ? card.cols_changed : Object.keys(card.after ?? card.before ?? {}).slice(0, 8)).map((c) => (
            <FieldRow key={c} label={c} a={card.before?.[c] == null ? null : String(card.before[c])}
                      b={card.after?.[c] == null ? null : String(card.after[c])} cls="text-fog" />
          ))}
        </div>
      )}
    </motion.article>
  );
}

export function Diff({ branch, mapping, canAct, replay }: { branch: Branch | null; mapping?: Record<string, string>; canAct: boolean; replay: boolean }) {
  const [filter, setFilter] = useState<"review" | "all">("review");
  const [col, setCol] = useState<string | null>(null);
  const d = branch?.diff;
  const bars = useMemo(() => {
    if (!d) return [];
    const entries = Object.entries(d.columns_touched).filter(([, n]) => n > 0).sort((a, b) => a[1] - b[1]);   // ASCENDING: the anomaly is the small number
    const max = Math.max(1, ...entries.map(([, n]) => n));
    return entries.map(([c, n]) => ({ c, n, w: n / max, rare: n <= Math.max(3, 0.01 * max) && entries.length > 1 }));
  }, [d]);
  const rows = useMemo(() => {
    if (!d || !branch) return [];
    let r = d.rows;
    if (col) r = r.filter((x) => x.cols_changed.includes(col));
    if (filter === "review") {
      const flagged = r.filter((x) => !(branch.approvals[x.key] ?? x.approved) || x.pair?.panel === "SPLIT" || (x.pair?.component_size ?? 2) > 2);
      if (flagged.length) r = flagged;
    }
    return r;
  }, [d, branch, filter, col]);
  const dropped = branch && (branch.status === "DISCARDED" || branch.status === "EXPIRED" || branch.status === "REJECTED");

  return (
    <Panel className="h-full"
      title={branch ? <span className="normal-case tracking-normal"><span className="font-mono text-fog">{branch.id}</span> <span className="text-mist">· {branch.cls || "branch"}</span></span> : "Diff viewer"}
      right={d && <div className="flex items-center gap-2">
        <Chip tone={d.mode === "KEYED" ? "mist" : "brass"}>{d.mode}</Chip>
        {d.base_drifted && <Chip tone="brass" title="GOLDEN moved since this branch copied it (another class merged); only drift on rows THIS branch touches blocks the merge">golden moved</Chip>}
        <span className="text-[11px] text-mist">{Math.round(d.diff_ms)} ms in-DB</span>
      </div>}>
      <div className="relative flex h-full flex-col">
        <>
          {dropped ? (
            <motion.div key={"dropped-" + branch!.id} initial={{ y: -40, rotate: -1 }} animate={{ y: 0, rotate: 0 }}
              transition={{ type: "spring", stiffness: 120, damping: 14 }} className="grid h-full place-items-center p-6 text-center">
              <div>
                <div className="text-lg font-semibold text-fog">{branch!.id} dropped</div>
                <div className="mt-1 text-sm text-mist">{branch!.discardedReason ?? branch!.rejected?.code}</div>
                <div className="mt-3 text-sm text-kelp">Nothing happened to GOLDEN — nothing was ever going to.</div>
              </div>
            </motion.div>
          ) : !d ? (
            <div key="empty" className="grid h-full place-items-center p-6 text-sm text-mist">
              {branch ? "Branch open. The diff appears when the agent (or you) asks for it." : "No branch yet."}
            </div>
          ) : (
            <div key={branch!.id + d.ts} className="flex min-h-0 flex-1 flex-col">
              <div className="grid grid-cols-3 gap-2 border-b border-rule px-3 py-2">
                {([["added", d.added, "text-sky"], ["changed", d.changed, "text-brass"], ["deleted", d.deleted, "text-flare"]] as const).map(([l, v, c]) => (
                  <div key={l}>
                    <CountUp value={v} className={`text-2xl font-bold ${c}`} />
                    <div className="text-[11px] uppercase tracking-wider text-mist">{l}{l === "changed" && d.mode === "KEYLESS" ? " (n/a keyless)" : ""}</div>
                  </div>
                ))}
              </div>
              {bars.length > 0 && (
                <div className="scroll-thin max-h-[96px] space-y-1 overflow-y-auto border-b border-rule px-3 py-1.5">
                  {bars.map(({ c, n, w, rare }) => (
                    <button key={c} onClick={() => setCol(col === c ? null : c)}
                      className={`grid w-full grid-cols-[130px_1fr_60px] items-center gap-2 text-left text-xs ${col === c ? "text-tide" : "text-fog"}`}>
                      <span className="truncate font-mono">{c} {rare && <span className="text-brass" title="touched on very few rows — look here first"><TriangleAlert className="inline h-3 w-3 -translate-y-px" /></span>}</span>
                      <span className="h-2.5 overflow-hidden rounded-sm bg-deck">
                        <motion.span className={`block h-full ${rare ? "bg-brass" : "bg-tide/70"}`} initial={{ width: 0 }} animate={{ width: `${Math.max(0.6, w * 100)}%` }} transition={{ duration: 0.8 }} />
                      </span>
                      <span className="num text-right">{fmt(n)}</span>
                    </button>
                  ))}
                </div>
              )}
              <div className="flex items-center gap-2 px-3 py-1.5 text-xs">
                <button onClick={() => setFilter("review")} className={filter === "review" ? "text-tide" : "text-mist"}>needs review</button>
                <span className="text-rule">|</span>
                <button onClick={() => setFilter("all")} className={filter === "all" ? "text-tide" : "text-mist"}>all shown rows</button>
                {col && <Chip tone="tide">{col} <button onClick={() => setCol(null)}>×</button></Chip>}
                <span className="ml-auto text-mist">{fmt(rows.length)} of {fmt(d.rows.length)} loaded · {fmt(d.rows_total)} in diff{replay ? " · replay" : ""}</span>
              </div>
              <div className="scroll-thin min-h-0 flex-1 space-y-2 overflow-y-auto px-3 pb-3">
                <AnimatePresence>
                  {rows.map((r) => <RowCard key={r.key} card={r} branch={branch!} mapping={mapping} canAct={canAct} />)}
                </AnimatePresence>
              </div>
            </div>
          )}
        </>
      </div>
    </Panel>
  );
}
