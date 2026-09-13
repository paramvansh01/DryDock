import { Check, ChevronDown, ChevronLeft, ChevronRight, CircleCheck, Database, ExternalLink, Filter, ListChecks,
  Scale, Search, ShieldAlert, Sparkles, TriangleAlert, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { type FilterKey, type MatchRow, type SortKey, FIELD_LABEL, compareFields, filterSort, matchRows,
  signalChips, sideValue } from "../derive";
import type { State } from "../types";
import { PanelVotes, Verdict } from "../ui";

const RISK_CLS = { High: "bg-flare/10 text-flare", Medium: "bg-brass/10 text-brass", Low: "bg-kelp/10 text-kelp" };
const SUG_CLS = { Match: "bg-kelp/10 text-kelp", Review: "bg-brass/10 text-brass", "No match": "bg-flare/10 text-flare" };

function Pill({ cls, children }: { cls: string; children: React.ReactNode }) {
  return <span className={`inline-flex whitespace-nowrap rounded-full px-2.5 py-0.5 text-[12px] font-medium ${cls}`}>{children}</span>;
}

function ScoreBar({ v }: { v: number }) {
  const tone = v >= 0.85 ? "bg-kelp" : v >= 0.6 ? "bg-brass" : "bg-flare";
  return (
    <div className="w-20">
      <div className="num text-[13px] font-semibold text-fog">{v.toFixed(2)}</div>
      <div className="mt-1 h-1.5 rounded-full bg-deck"><div className={`h-full rounded-full ${tone}`} style={{ width: `${Math.round(v * 100)}%` }} /></div>
    </div>
  );
}

function name(r: MatchRow, side: "a" | "b", mapping?: Record<string, string>) {
  return sideValue("FULL_NAME", side, r.pair, mapping) ?? "—";
}
function email(r: MatchRow, side: "a" | "b", mapping?: Record<string, string>) {
  return sideValue("EMAIL", side, r.pair, mapping) ?? "no email";
}

export function Reconcile({ s, canAct, onOpenDiff }: { s: State; canAct: boolean; onOpenDiff: (branchId: string) => void }) {
  const all = useMemo(() => matchRows(s), [s]);
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<FilterKey>("all");
  const [sort, setSort] = useState<SortKey>("risk");
  const [page, setPage] = useState(1);
  const [per, setPer] = useState(5);
  const [sel, setSel] = useState<Set<string>>(new Set());
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const rows = useMemo(() => filterSort(all, q, filter, sort), [all, q, filter, sort]);
  const pages = Math.max(1, Math.ceil(rows.length / per));
  useEffect(() => { if (page > pages) setPage(1); }, [pages, page]);
  const shown = rows.slice((page - 1) * per, page * per);
  const open = rows.find((r) => r.key === openKey) ?? rows[0] ?? null;
  const openIdx = open ? rows.indexOf(open) : -1;
  const loadedTotal = Object.values(s.branches).reduce((t, b) => t + (b.diff?.rows_total ?? 0), 0);

  const decide = async (items: MatchRow[], approved: boolean) => {
    if (!canAct || !items.length) return;
    setBusy(true);
    const byBranch = new Map<string, string[]>();
    for (const r of items) byBranch.set(r.branchId, [...(byBranch.get(r.branchId) ?? []), r.key]);
    try {
      for (const [bid, keys] of byBranch) await api.setRows(bid, keys, approved);
    } catch (e) { alert(String(e)); }
    setBusy(false);
  };
  const editable = (r: MatchRow) => canAct && r.branchStatus === "PENDING";

  return (
    <div className="grid grid-cols-[minmax(0,1fr)_420px] gap-4">
      {/* ---------------- table */}
      <section className="card flex min-w-0 flex-col">
        <header className="flex items-start justify-between gap-4 px-5 pb-3 pt-4">
          <div className="flex items-start gap-3">
            <ListChecks className="mt-0.5 h-6 w-6 text-navy" />
            <div>
              <h2 className="text-xl font-bold text-fog">Candidate Matches</h2>
              <p className="text-[13px] text-mist">Review the potential matches below. Accept, reject, or open details to see the differences.</p>
            </div>
          </div>
          <div className="text-right">
            <div className="num text-[15px] font-semibold text-fog">{rows.length.toLocaleString("en-GB")} pairs</div>
            <div className="text-[11px] text-mist">{loadedTotal ? `loaded from ${loadedTotal.toLocaleString("en-GB")} diff rows` : "no diff yet"}</div>
          </div>
        </header>
        <div className="flex items-center gap-3 px-5 pb-3">
          <label className="flex flex-1 items-center gap-2 rounded-lg border border-rule px-3 py-2 text-[13px] focus-within:border-tide">
            <Search className="h-4 w-4 text-mist" />
            <input value={q} onChange={(e) => { setQ(e.target.value); setPage(1); }} placeholder="Search by name, email, ID…"
              className="w-full bg-transparent outline-none placeholder:text-mist" />
          </label>
          <label className="flex items-center gap-2 rounded-lg border border-rule px-3 py-2 text-[13px]">
            <Filter className="h-4 w-4 text-mist" />
            <select value={filter} onChange={(e) => { setFilter(e.target.value as FilterKey); setPage(1); }} className="bg-transparent outline-none">
              <option value="all">All</option><option value="review">Needs review</option><option value="split">Split votes</option>
              <option value="accepted">Accepted</option><option value="rejected">Rejected</option>
            </select>
          </label>
          <span className="text-[13px] text-mist">Sort by</span>
          <select value={sort} onChange={(e) => setSort(e.target.value as SortKey)} className="rounded-lg border border-rule px-3 py-2 text-[13px] outline-none">
            <option value="risk">Risk (high → low)</option><option value="score_desc">Score (high → low)</option><option value="score_asc">Score (low → high)</option>
          </select>
        </div>
        {sel.size > 0 && (
          <div className="flex items-center gap-3 border-y border-rule bg-tide/5 px-5 py-2 text-[13px]">
            <span className="font-medium text-fog">{sel.size} selected</span>
            <button disabled={busy || !canAct} onClick={() => decide(all.filter((r) => sel.has(r.key) && editable(r)), true)} className="text-kelp disabled:opacity-40">Accept</button>
            <button disabled={busy || !canAct} onClick={() => decide(all.filter((r) => sel.has(r.key) && editable(r)), false)} className="text-flare disabled:opacity-40">Reject</button>
            <button onClick={() => setSel(new Set())} className="ml-auto text-mist">Clear</button>
          </div>
        )}
        <div className="min-h-0 flex-1 overflow-x-auto">
          <table className="w-full text-left text-[13px]">
            <thead className="bg-deck/70 text-[12px] text-fog/80">
              <tr>
                <th className="w-10 py-2.5 pl-4 pr-1"><input type="checkbox" className="h-4 w-4 accent-[var(--color-tide)]"
                  checked={shown.length > 0 && shown.every((r) => sel.has(r.key))}
                  onChange={(e) => setSel((p) => { const n = new Set(p); shown.forEach((r) => (e.target.checked ? n.add(r.key) : n.delete(r.key))); return n; })} /></th>
                <th className="px-2 font-medium">#</th>
                <th className="px-2 font-medium">SOURCE_A</th>
                <th className="px-2 font-medium">SOURCE_B</th>
                <th className="px-2 font-medium">Match Score</th>
                <th className="px-2 font-medium">Suggested</th>
                <th className="px-2 font-medium" title="High: matchers split or cluster > 2 · Medium: unanimous with a conflict signal or score < 0.90 · Low: unanimous, clean">Risk</th>
                <th className="px-2 text-center font-medium">Action</th>
              </tr>
            </thead>
            <tbody>
              {shown.length === 0 && (
                <tr><td colSpan={8} className="px-5 py-10 text-center text-mist">
                  {all.length === 0 ? "No candidate pairs yet. They appear here when a match-class branch is diffed." : "No pairs match this filter."}
                </td></tr>
              )}
              {shown.map((r, i) => {
                const n = (page - 1) * per + i + 1;
                const active = open?.key === r.key;
                return (
                  <tr key={r.key} onClick={() => setOpenKey(r.key)}
                    className={`cursor-pointer border-t border-rule ${active ? "bg-tide/5" : "hover:bg-deck/50"}`}>
                    <td className="py-3 pl-4 pr-1" onClick={(e) => e.stopPropagation()}>
                      <input type="checkbox" className="h-4 w-4 accent-[var(--color-tide)]" checked={sel.has(r.key)}
                        onChange={(e) => setSel((p) => { const s2 = new Set(p); e.target.checked ? s2.add(r.key) : s2.delete(r.key); return s2; })} />
                    </td>
                    <td className="px-2 text-mist">{n}</td>
                    <td className="max-w-[190px] px-2 py-3">
                      <div className="truncate font-semibold text-fog">{name(r, "a", s.mapping)}</div>
                      <div className="text-[12px] text-mist">{r.pair.a_id}</div>
                      <div className="truncate text-[12px] text-mist">{email(r, "a", s.mapping)}</div>
                    </td>
                    <td className="max-w-[190px] px-2 py-3">
                      <div className="truncate font-semibold text-fog">{name(r, "b", s.mapping)}</div>
                      <div className="text-[12px] text-mist">{r.pair.b_id}</div>
                      <div className="truncate text-[12px] text-mist">{email(r, "b", s.mapping)}</div>
                    </td>
                    <td className="px-2"><ScoreBar v={r.pair.score} /></td>
                    <td className="px-2"><Pill cls={SUG_CLS[r.suggested]}>{r.suggested}</Pill></td>
                    <td className="px-2"><Pill cls={RISK_CLS[r.risk]}>{r.risk}</Pill></td>
                    <td className="px-2" onClick={(e) => e.stopPropagation()}>
                      <div className="flex items-center justify-center gap-1 pr-3">
                        <button title={editable(r) ? "Accept: include in the merge" : "read-only: branch not pending review"}
                          disabled={!editable(r) || busy} onClick={() => decide([r], true)}
                          className={`grid h-7 w-7 place-items-center rounded-md border ${r.approved ? "border-kelp bg-kelp text-white" : "border-rule text-kelp"} disabled:cursor-not-allowed disabled:opacity-60`}>
                          <Check className="h-4 w-4" />
                        </button>
                        <button title={editable(r) ? "Reject: keep GOLDEN's row" : "read-only: branch not pending review"}
                          disabled={!editable(r) || busy} onClick={() => decide([r], false)}
                          className={`grid h-7 w-7 place-items-center rounded-md border ${!r.approved ? "border-flare bg-flare text-white" : "border-rule text-flare"} disabled:cursor-not-allowed disabled:opacity-60`}>
                          <X className="h-4 w-4" />
                        </button>
                        <button title="Open in Diff Viewer" onClick={() => onOpenDiff(r.branchId)}
                          className="grid h-7 w-7 place-items-center rounded-md border border-rule text-fog/70 hover:text-tide">
                          <ExternalLink className="h-4 w-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <footer className="flex items-center justify-between border-t border-rule px-5 py-3 text-[13px] text-mist">
          <span>Showing {rows.length ? (page - 1) * per + 1 : 0}–{Math.min(page * per, rows.length)} of {rows.length.toLocaleString("en-GB")}</span>
          <Pager page={page} pages={pages} onPage={setPage} />
          <label className="flex items-center gap-2">Rows per page
            <select value={per} onChange={(e) => { setPer(Number(e.target.value)); setPage(1); }} className="rounded-md border border-rule px-2 py-1 text-fog">
              {[5, 10, 25, 50].map((n) => <option key={n}>{n}</option>)}
            </select>
          </label>
        </footer>
      </section>

      {/* ---------------- pair detail */}
      <PairDetail s={s} row={open} idx={openIdx} total={rows.length} busy={busy} editable={open ? editable(open) : false}
        onPrev={() => openIdx > 0 && setOpenKey(rows[openIdx - 1].key)}
        onNext={() => openIdx < rows.length - 1 && setOpenKey(rows[openIdx + 1].key)}
        onClose={() => setOpenKey(null)} onDecide={(ok) => open && decide([open], ok)} onOpenDiff={onOpenDiff} />
    </div>
  );
}

function Pager({ page, pages, onPage }: { page: number; pages: number; onPage: (p: number) => void }) {
  const nums = pages <= 6 ? Array.from({ length: pages }, (_, i) => i + 1)
    : [...new Set([1, 2, 3, Math.max(1, page - 1), page, Math.min(pages, page + 1), pages])].filter((n) => n >= 1 && n <= pages).sort((a, b) => a - b);
  return (
    <div className="flex items-center gap-1">
      <button onClick={() => onPage(Math.max(1, page - 1))} className="grid h-8 w-8 place-items-center rounded-md border border-rule"><ChevronLeft className="h-4 w-4" /></button>
      {nums.map((n, i) => (
        <span key={n} className="flex items-center">
          {i > 0 && n - nums[i - 1] > 1 && <span className="px-1">…</span>}
          <button onClick={() => onPage(n)} className={`h-8 min-w-8 rounded-md px-2 ${n === page ? "bg-navy text-white" : "text-fog"}`}>{n}</button>
        </span>
      ))}
      <button onClick={() => onPage(Math.min(pages, page + 1))} className="grid h-8 w-8 place-items-center rounded-md border border-rule"><ChevronRight className="h-4 w-4" /></button>
    </div>
  );
}

function PairDetail({ s, row, idx, total, busy, editable, onPrev, onNext, onClose, onDecide, onOpenDiff }: {
  s: State; row: MatchRow | null; idx: number; total: number; busy: boolean; editable: boolean;
  onPrev: () => void; onNext: () => void; onClose: () => void; onDecide: (ok: boolean) => void; onOpenDiff: (b: string) => void;
}) {
  const [showDiffs, setShowDiffs] = useState(false);
  if (!row) {
    return <aside className="card grid place-items-center p-8 text-center text-[13px] text-mist">Select a pair to see both records side by side.</aside>;
  }
  const p = row.pair;
  const cmp = compareFields(p, s.mapping);
  const banner = row.risk === "Low"
    ? { cls: "border-kelp/30 bg-kelp/5", icon: <CircleCheck className="h-6 w-6 text-kelp" />, title: "High confidence match", sub: "All three matchers agree and no conflicting signal fired." }
    : row.risk === "Medium"
      ? { cls: "border-brass/30 bg-brass/5", icon: <TriangleAlert className="h-6 w-6 text-brass" />, title: "Likely match — check the flagged signal", sub: "The matchers agree, but a conflicting signal is present." }
      : { cls: "border-flare/30 bg-flare/5", icon: <ShieldAlert className="h-6 w-6 text-flare" />, title: "Needs a human decision", sub: p.panel === "SPLIT" ? "The three matchers disagreed on this pair." : `Part of a cluster of ${p.component_size} records.` };
  const extra = (side: "a" | "b") => {
    const rec = side === "a" ? p.a : p.b;
    const since = side === "a" ? rec?.CREATED_AT : (p.kind === "AA" ? rec?.CREATED_AT : rec?.LAST_SEEN);
    return since ? String(since).slice(0, 10) : "—";
  };
  const rows: [string, (side: "a" | "b") => string][] = [
    ["ID", (sd) => (sd === "a" ? p.a_id : p.b_id)],
    ...(["FULL_NAME", "EMAIL", "PHONE", "ADDR_LINE"] as const).map((f) =>
      [FIELD_LABEL[f], (sd: "a" | "b") => sideValue(f, sd, p, s.mapping) ?? "—"] as [string, (sd: "a" | "b") => string]),
  ];
  const dob = (sd: "a" | "b") => sideValue("DATE_OF_BIRTH", sd, p, s.mapping) ?? "—";
  const cell = (f: string, sd: "a" | "b") => cmp.find((c) => FIELD_LABEL[c.field] === f)?.state;

  return (
    <aside className="card flex flex-col">
      <header className="flex items-center justify-between px-5 pb-3 pt-4">
        <h3 className="text-xl font-bold text-fog">Pair #{idx + 1}</h3>
        <div className="flex items-center gap-2 text-[13px] text-mist">
          <button onClick={onPrev} className="grid h-7 w-7 place-items-center rounded-md border border-rule"><ChevronLeft className="h-4 w-4" /></button>
          <button onClick={onNext} className="grid h-7 w-7 place-items-center rounded-md border border-rule"><ChevronRight className="h-4 w-4" /></button>
          <span className="num">{idx + 1} of {total.toLocaleString("en-GB")}</span>
          <button onClick={onClose} className="ml-2 text-mist hover:text-fog"><X className="h-5 w-5" /></button>
        </div>
      </header>
      <div className="space-y-4 px-5 pb-5">
        <div className={`flex items-center gap-3 rounded-lg border px-4 py-3 ${banner.cls}`}>
          {banner.icon}
          <div className="flex-1">
            <div className="text-[14px] font-semibold text-fog">{banner.title}</div>
            <div className="text-[12px] text-mist">{banner.sub}</div>
          </div>
          <span className="rounded-full bg-hull px-3 py-1 text-[12px] font-semibold text-fog shadow-sm">Match score {p.score.toFixed(2)}</span>
        </div>

        <div className="grid grid-cols-2 gap-2">
          {(["a", "b"] as const).map((sd) => (
            <div key={sd} className="overflow-hidden rounded-lg border border-rule">
              <div className={`flex items-center gap-2 px-3 py-2 text-[13px] font-semibold ${sd === "a" ? "bg-tide/5 text-fog" : "bg-sky/5 text-fog"}`}>
                <Database className={`h-4 w-4 ${sd === "a" ? "text-tide" : "text-sky"}`} />
                {sd === "a" ? "SOURCE_A" : p.kind === "AA" ? "SOURCE_A (duplicate)" : "SOURCE_B"}
              </div>
              <dl className="grid grid-cols-[50px_minmax(0,1fr)] gap-x-2 gap-y-1.5 px-3 py-2.5 text-[12px]">
                {rows.map(([label, get]) => {
                  const st = cell(label, sd);
                  return (
                    <div key={label} className="contents">
                      <dt className="text-mist">{label}</dt>
                      <dd className={`truncate ${st === "diff" ? "text-brass" : "text-fog"}`} title={get(sd)}>{get(sd)}</dd>
                    </div>
                  );
                })}
                <div className="col-span-2 my-1 border-t border-rule" />
                <dt className="text-mist">DOB</dt><dd className={cell("DOB", sd) === "diff" ? "text-brass" : "text-fog"}>{dob(sd)}</dd>
                <dt className="text-mist">{sd === "a" || p.kind === "AA" ? "Created" : "Last seen"}</dt><dd className="text-fog">{extra(sd)}</dd>
              </dl>
            </div>
          ))}
        </div>

        <div>
          <div className="mb-2 flex items-center gap-2 text-[13px] font-semibold text-fog"><Sparkles className="h-4 w-4 text-navy" /> Match Signals</div>
          <div className="flex flex-wrap gap-2">
            {signalChips(p).map((c) => (
              <span key={c.label} className={`rounded-full border px-2.5 py-1 text-[12px] ${c.tone === "good" ? "border-rule bg-deck text-fog" : c.tone === "bad" ? "border-flare/30 bg-flare/5 text-flare" : "border-rule bg-hull text-mist"}`}>{c.label}</span>
            ))}
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <PanelVotes votes={p.votes} split={p.panel === "SPLIT"} />
            {p.ai_verdict && <Verdict verdict={p.ai_verdict} score={p.ai_score} adjudicator={p.adjudicator} rationale={p.rationale} />}
          </div>
          {p.precedents.map((pr) => (
            <div key={pr.id} className="mt-2 rounded-md border border-tide/20 bg-tide/5 px-3 py-1.5 text-[12px] text-fog">
              <Scale className="mr-1 inline h-3.5 w-3.5 -translate-y-px text-tide" />Precedent #{pr.id} <span className={pr.verdict === "REJECTED" ? "text-flare" : "text-kelp"}>{pr.verdict}</span>{pr.note ? ` — “${pr.note}”` : ""}
            </div>
          ))}
        </div>

        <button onClick={() => setShowDiffs((v) => !v)} className="flex w-full items-center justify-between border-t border-rule pt-3 text-[13px] font-semibold text-fog">
          <span>Field Differences ({row.diffs})</span><ChevronDown className={`h-4 w-4 transition-transform ${showDiffs ? "rotate-180" : ""}`} />
        </button>
        {showDiffs && (
          <table className="w-full text-[12.5px]">
            <tbody>
              {cmp.filter((c) => c.state !== "same").map((c) => (
                <tr key={c.field} className="border-t border-rule">
                  <td className="py-1 pr-2 text-mist">{FIELD_LABEL[c.field]}</td>
                  <td className="py-1 pr-2 text-fog">{c.a ?? "∅"}</td>
                  <td className="py-1 text-fog">{c.b ?? "∅"}</td>
                </tr>
              ))}
              {cmp.every((c) => c.state === "same") && <tr><td className="py-1 text-mist">No differing fields.</td></tr>}
            </tbody>
          </table>
        )}

        <div className="grid grid-cols-3 gap-2 pt-1">
          <button disabled={!editable || busy} onClick={() => onDecide(true)}
            className="flex items-center justify-center gap-2 rounded-lg bg-kelp px-3 py-2.5 text-[14px] font-semibold text-white disabled:opacity-40">
            <Sparkles className="h-4 w-4" /> Accept Match
          </button>
          <button disabled={!editable || busy} onClick={() => onDecide(false)}
            className="flex items-center justify-center gap-2 rounded-lg border border-flare px-3 py-2.5 text-[14px] font-semibold text-flare disabled:opacity-40">
            <X className="h-4 w-4" /> Reject Match
          </button>
          <button onClick={() => onOpenDiff(row.branchId)}
            className="flex items-center justify-center gap-2 rounded-lg border border-rule px-3 py-2.5 text-[14px] font-medium text-fog">
            <ExternalLink className="h-4 w-4" /> Open in Diff Viewer
          </button>
        </div>
        {!editable && <p className="text-center text-[11px] text-mist">
          {row.branchStatus === "PENDING" ? "Read-only while not connected live." : `Decisions apply while the ${row.branchId} merge is pending review (now ${row.branchStatus.toLowerCase()}).`}
        </p>}
      </div>
    </aside>
  );
}
