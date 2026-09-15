// Database: a read-only browser over the Exasol instance — the object tree on the left, one
// table at a time on the right, its rows numbered like lines in an editor.
//
// Two sources, kept apart on purpose:
//   the tree, row counts, columns and keys come from db.snapshot on the event stream (what Exasol
//     reported at snapshot time), and
//   a page of rows is read when you ask for it, through GET /db/rows on the orchestrator — the
//     browser sends no SQL, it names a table, a page, a sort column and a filter. The statement the
//     orchestrator composed is shown under every grid.
// A replay has no live database behind it, so the grid says so instead of showing rows.

import {
  ArrowDown, ArrowUp, ChevronDown, ChevronRight, Copy, Database, Download, Eye, Gem, GitBranch, History, KeyRound,
  Layers, Lock, RefreshCw, Rows3, Search, Settings2, Table2, Terminal, Upload, X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, downloads } from "../api";
import type { Page, SnapTable, State } from "../types";
import { fmt, short } from "../ui";
import { HistoryDrawer } from "./History";
import { keysOf } from "./System";

// ------------------------------------------------------------------ what each schema is

type Meta = { note: string; icon: React.ElementType; color: string; locked?: boolean };

const SCHEMA: Record<string, Meta> = {
  GOLDEN: { note: "production · written only through the merge gate", icon: Gem, color: "#b45309" },
  GOLDEN_V: { note: "the view the agent reads GOLDEN through", icon: Eye, color: "#0d9488" },
  SOURCE_A: { note: "input system · read-only", icon: Table2, color: "#2563eb" },
  SOURCE_B: { note: "input system · read-only", icon: Table2, color: "#7c3aed" },
  UPLOADS: { note: "your own two files · read-only to every branch", icon: Upload, color: "#0d9488" },
  ER_WORK: { note: "each run's published match decisions", icon: Layers, color: "#7c3aed" },
  DRYDOCK: { note: "control plane · runs, diffs, merges, events", icon: Settings2, color: "#1e3a5f" },
  BENCH: { note: "answer key · never in a prompt, no agent grant", icon: Lock, color: "#dc2626", locked: true },
};
const BRANCH = { note: "branch copy · copy-on-write, dropped on discard", icon: GitBranch, color: "#2563eb" };
const meta = (schema: string): Meta => SCHEMA[schema] ?? BRANCH;

// The copies drydock/merge.py makes around a swap. They are the merge's working state, not tables
// to browse — and unlike a blanket "__" rule, this hides only them.
const WORKING_COPY = /__(NEW|ARCH|UNDONE)_/;

// Pipeline order, so the tree reads the way the data flows rather than alphabetically.
const ORDER = ["GOLDEN", "GOLDEN_V", "UPLOADS", "SOURCE_A", "SOURCE_B", "ER_WORK", "DRYDOCK", "BENCH"];
const rank = (s: string) => (ORDER.includes(s) ? ORDER.indexOf(s) : ORDER.length + (s.startsWith("BR_") ? 0 : 1));

// ------------------------------------------------------------------ column types

const NUMERIC = /^(DECIMAL|DOUBLE|INTEGER|BIGINT|NUMBER|FLOAT)/;
const TEMPORAL = /^(DATE|TIMESTAMP|INTERVAL)/;
const typeLabel = (t: string) => t.replace(" UTF8", "").toLowerCase();

export function DatabaseView({ s, replay }: { s: State; replay: boolean }) {
  const snap = s.system.snapshot;
  const [sel, setSel] = useState<string | null>(null);          // "SCHEMA.TABLE"; null = the overview
  const [find, setFind] = useState("");

  const tables = snap?.tables ?? [];
  const groups = useMemo(() => {
    const by: Record<string, SnapTable[]> = {};
    for (const t of tables) {
      if (WORKING_COPY.test(t.table)) continue;                 // a merge's own __NEW_/__ARCH_/__UNDONE_ copies
      (by[t.schema] ??= []).push(t);
    }
    return Object.entries(by)
      .map(([schema, list]) => ({ schema, list: [...list].sort((a, b) => a.table.localeCompare(b.table)) }))
      .sort((a, b) => rank(a.schema) - rank(b.schema) || a.schema.localeCompare(b.schema));
  }, [tables]);

  const picked = sel ? tables.find((t) => `${t.schema}.${t.table}` === sel) ?? null : null;
  const [history, setHistory] = useState<string | null>(null);
  const [lookup, setLookup] = useState("");

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-3">
        <Database className="mt-1 h-6 w-6 text-navy" />
        <div className="min-w-0">
          <h1 className="text-2xl font-bold text-fog">Database</h1>
          <p className="text-[13px] text-mist">
            Every schema Drydock touches, as Exasol reports it — open a table to read its rows, its columns and its keys.
            Read-only: this view has no verb that writes.
          </p>
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2">
          <form data-tour="db-lookup" onSubmit={(e) => { e.preventDefault(); if (lookup.trim()) setHistory(lookup.trim()); }}
            className="flex items-center gap-1.5 rounded-lg border border-rule bg-hull px-2.5 py-1.5"
            title="Type a clean-list id (e.g. G-A-1001) to see where that customer's details came from and every change">
            <History className="h-4 w-4 text-mist" />
            <input value={lookup} onChange={(e) => setLookup(e.target.value)} placeholder="Customer history: G-…"
              className="w-[170px] bg-transparent text-[12.5px] outline-none placeholder:text-mist" />
          </form>
          <a href={downloads.golden} data-tour="db-download" title="The reconciled list as it stands now, as a CSV file"
            className="flex items-center gap-1.5 rounded-lg bg-navy px-3.5 py-2 text-[12.5px] font-semibold text-white shadow-sm">
            <Download className="h-4 w-4" /> Download clean list
          </a>
          {snap && (
            <div className="ml-2 text-right text-[11.5px] leading-tight text-mist">
              <div>Exasol {snap.version}</div>
              <div className="font-mono">session …{snap.session.slice(-6)} · read in {snap.ms} ms</div>
            </div>
          )}
        </div>
      </div>

      <div className="grid h-[calc(100vh-232px)] min-h-[520px] grid-cols-[286px_1fr] gap-4">
        <Tree groups={groups} sel={sel} onSel={setSel} find={find} onFind={setFind} />
        {picked ? (
          <TablePane key={sel} s={s} t={picked} replay={replay} onHistory={setHistory} />
        ) : (
          <Overview s={s} groups={groups} onOpen={setSel} />
        )}
      </div>
      {history && <HistoryDrawer goldenId={history} onClose={() => setHistory(null)} />}
    </div>
  );
}

// ------------------------------------------------------------------ object tree

function Tree({ groups, sel, onSel, find, onFind }: {
  groups: { schema: string; list: SnapTable[] }[]; sel: string | null; onSel: (fq: string | null) => void;
  find: string; onFind: (v: string) => void;
}) {
  const [shut, setShut] = useState<Record<string, boolean>>({});
  const q = find.trim().toUpperCase();
  const shown = groups
    .map((g) => ({ ...g, list: q ? g.list.filter((t) => `${t.schema}.${t.table}`.includes(q)) : g.list }))
    .filter((g) => g.list.length > 0);

  return (
    <aside className="card flex min-h-0 flex-col overflow-hidden" data-tour="db-tree">
      <div className="border-b border-rule px-3 py-2.5">
        <div className="flex items-center gap-2 rounded-lg border border-rule bg-deck/60 px-2.5 py-1.5 focus-within:border-tide/50">
          <Search className="h-3.5 w-3.5 shrink-0 text-mist" />
          <input value={find} onChange={(e) => onFind(e.target.value)} placeholder="Find a table…"
            className="w-full bg-transparent text-[12.5px] text-fog outline-none placeholder:text-mist" />
          {find && <button onClick={() => onFind("")} className="text-mist hover:text-fog"><X className="h-3.5 w-3.5" /></button>}
        </div>
      </div>

      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto py-1.5">
        <button onClick={() => onSel(null)}
          className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] ${sel === null ? "bg-tide/5 font-medium text-tide" : "text-fog/85 hover:bg-deck/60"}`}>
          <Database className="h-3.5 w-3.5 shrink-0" /> Storage overview
        </button>

        {shown.length === 0 && <div className="px-3 py-6 text-[12.5px] text-mist">{q ? "No table matches." : "Waiting for Exasol to report its catalogue…"}</div>}

        {shown.map(({ schema, list }) => {
          const m = meta(schema);
          const open = !shut[schema] || !!q;
          const rows = list.reduce<number | null>((a, t) => (a == null || t.rows == null ? a : a + t.rows), 0);
          return (
            <div key={schema} className="mt-0.5">
              <button onClick={() => setShut((v) => ({ ...v, [schema]: !v[schema] }))}
                className="group flex w-full items-center gap-1.5 px-2 py-1.5 text-left hover:bg-deck/60">
                {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0 text-mist" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0 text-mist" />}
                <m.icon className="h-3.5 w-3.5 shrink-0" style={{ color: m.color }} />
                <span className="truncate font-mono text-[12px] font-semibold text-fog" title={m.note}>{schema}</span>
                <span className="num ml-auto shrink-0 pr-1 text-[10.5px] text-mist">{list.length}</span>
              </button>
              {open && (
                <ul>
                  {list.map((t) => {
                    const fq = `${t.schema}.${t.table}`;
                    const on = sel === fq;
                    return (
                      <li key={fq}>
                        <button onClick={() => onSel(fq)} title={`${fq}${t.rows == null ? " (view)" : ` · ${fmt(t.rows)} rows`}`}
                          className={`flex w-full items-center gap-2 border-l-2 py-[5px] pl-[26px] pr-2 text-left transition-colors
                            ${on ? "border-tide bg-tide/5" : "border-transparent hover:bg-deck/60"}`}>
                          {t.kind === "VIEW" ? <Eye className="h-3 w-3 shrink-0 text-[#0d9488]" /> : <Table2 className="h-3 w-3 shrink-0 text-mist" />}
                          <span className={`truncate font-mono text-[11.5px] ${on ? "font-semibold text-tide" : "text-fog/85"}`}>{t.table}</span>
                          <span className="num ml-auto shrink-0 text-[10.5px] text-mist">{t.rows == null ? "view" : fmt(t.rows)}</span>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          );
        })}
      </div>

      <div className="border-t border-rule px-3 py-2 text-[10.5px] leading-snug text-mist">
        Tree and row counts come from the event stream's latest catalogue read.
      </div>
    </aside>
  );
}

// ------------------------------------------------------------------ storage overview (no table selected)

function Overview({ s, groups, onOpen }: {
  s: State; groups: { schema: string; list: SnapTable[] }[]; onOpen: (fq: string) => void;
}) {
  const mats = s.order.map((id) => s.branches[id]).filter((b) => b.materialised);
  return (
    <div className="scroll-thin min-h-0 space-y-4 overflow-y-auto pr-0.5">
      <div className="grid grid-cols-3 gap-3">
        {groups.map(({ schema, list }) => {
          const m = meta(schema);
          const rows = list.reduce((a, t) => a + (t.rows ?? 0), 0);
          return (
            <button key={schema} onClick={() => list[0] && onOpen(`${schema}.${list[0].table}`)}
              className="card px-4 py-3 text-left transition-colors hover:border-tide/40">
              <div className="flex items-center gap-2">
                <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg" style={{ background: `${m.color}14` }}>
                  <m.icon className="h-4 w-4" style={{ color: m.color }} />
                </span>
                <span className="truncate font-mono text-[13px] font-semibold text-fog">{schema}</span>
                {m.locked && <Lock className="ml-auto h-3.5 w-3.5 text-flare" />}
              </div>
              <div className="mt-2 flex items-baseline gap-1.5">
                <span className="num text-xl font-bold text-fog">{fmt(rows)}</span>
                <span className="text-[11.5px] text-mist">rows in {list.length} table{list.length === 1 ? "" : "s"}</span>
              </div>
              <div className="mt-0.5 text-[11px] leading-snug text-mist">{m.note}</div>
            </button>
          );
        })}
      </div>

      <div className="grid grid-cols-2 gap-4">
        <section className="card px-5 py-4">
          <div className="text-[13px] font-semibold text-fog">GOLDEN.CUSTOMERS</div>
          <div className="mt-2 flex items-baseline gap-4">
            <span className="num text-3xl font-bold text-fog">{fmt(s.golden.rows)}</span><span className="text-mist">rows</span>
            <span className="font-mono text-[13px] text-fog" title={s.golden.fingerprint ?? ""}>fp {short(s.golden.fingerprint)}</span>
          </div>
          <table className="mt-4 w-full text-[12.5px]">
            <thead className="text-left text-mist"><tr><th className="py-1 font-medium">Time</th><th className="font-medium">Fingerprint</th><th className="font-medium">Cause</th></tr></thead>
            <tbody>
              {s.golden.history.length === 0 && <tr><td colSpan={3} className="py-3 text-mist">No fingerprint observed yet.</td></tr>}
              {[...s.golden.history].reverse().map((h, i) => (
                <tr key={i} className="border-t border-rule"><td className="py-1.5">{new Date(h.ts).toLocaleTimeString("en-GB")}</td>
                  <td className="font-mono">{short(h.fingerprint)}</td><td>{h.cause}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
        <section className="card px-5 py-4">
          <div className="text-[13px] font-semibold text-fog">Branch copies (copy-on-write)</div>
          <table className="mt-3 w-full text-[12.5px]">
            <thead className="text-left text-mist"><tr><th className="py-1 font-medium">Branch</th><th className="font-medium">Table</th>
              <th className="text-right font-medium">Rows</th><th className="text-right font-medium">Copy</th><th className="pl-3 font-medium">Status</th></tr></thead>
            <tbody>
              {mats.length === 0 && <tr><td colSpan={5} className="py-3 text-mist">No table has been copied into a branch yet.</td></tr>}
              {mats.map((b) => (
                <tr key={b.id} className="border-t border-rule">
                  <td className="py-1.5 font-mono">{b.id}</td><td>{b.materialised!.table}</td>
                  <td className="num text-right">{fmt(b.materialised!.rows)}</td><td className="num text-right">{Math.round(b.materialised!.copy_ms)} ms</td>
                  <td className="pl-3">{b.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ one table

const SIZES = [25, 50, 100, 200];

function TablePane({ s, t, replay, onHistory }: { s: State; t: SnapTable; replay: boolean; onHistory: (id: string) => void }) {
  const fq = `${t.schema}.${t.table}`;
  const m = meta(t.schema);
  const snap = s.system.snapshot;
  const keys = keysOf(snap, fq);
  const snapCols = snap?.columns[fq] ?? [];

  const [tab, setTab] = useState<"rows" | "structure">("rows");
  const [limit, setLimit] = useState(50);
  const [offset, setOffset] = useState(0);
  const [sort, setSort] = useState<{ col: string; dir: "ASC" | "DESC" } | null>(null);
  const [typed, setTyped] = useState("");
  const [q, setQ] = useState("");
  const [page, setPage] = useState<Page | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [row, setRow] = useState<number | null>(null);
  const [showSql, setShowSql] = useState(false);
  const [nonce, setNonce] = useState(0);

  useEffect(() => { const h = setTimeout(() => { setQ(typed); setOffset(0); }, 300); return () => clearTimeout(h); }, [typed]);

  const seq = useRef(0);
  const load = useCallback(async () => {
    const mine = ++seq.current;        // a page that arrives late must not overwrite a newer one
    setBusy(true);
    setErr(null);
    try {
      const p = await api.browse({ schema: t.schema, table: t.table, limit, offset, order: sort?.col ?? null, dir: sort?.dir, q });
      if (mine === seq.current) setPage(p);
    } catch (e) {
      if (mine === seq.current) { setPage(null); setErr(String(e instanceof Error ? e.message : e)); }
    }
    if (mine === seq.current) setBusy(false);
  }, [t.schema, t.table, limit, offset, sort?.col, sort?.dir, q]);

  useEffect(() => { if (!replay) { setRow(null); load(); } }, [load, replay, nonce]);

  const cols = page?.columns ?? snapCols;
  const total = page?.total ?? t.rows;             // with a filter on, the size of the filtered set
  // The header states the table's own size, so a filter never looks like rows disappearing from it.
  const tableRows = page && !page.q ? page.total : t.rows;
  const shown = page?.rows.length ?? 0;
  const from = shown === 0 ? 0 : offset + 1;
  const to = offset + shown;
  const last = total != null && offset + limit >= total;

  const clickCol = (c: string) => {
    setOffset(0);
    setSort((v) => (v?.col !== c ? { col: c, dir: "ASC" } : v.dir === "ASC" ? { col: c, dir: "DESC" } : null));
  };

  return (
    <section className="flex min-h-0 flex-col gap-3">
      <header className="card shrink-0 px-5 py-3.5">
        <div className="flex items-center gap-3">
          <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl" style={{ background: `${m.color}14` }}>
            <m.icon className="h-5 w-5" style={{ color: m.color }} />
          </span>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h2 className="truncate font-mono text-[17px] font-bold text-fog"><span className="text-mist">{t.schema}.</span>{t.table}</h2>
              <span className="rounded-md bg-deck px-1.5 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide text-mist">{t.kind}</span>
              {m.locked && <span className="flex items-center gap-1 rounded-md bg-flare/5 px-1.5 py-0.5 text-[10.5px] font-medium text-flare"><Lock className="h-3 w-3" />answer key</span>}
            </div>
            <div className="truncate text-[11.5px] text-mist">{m.note}</div>
          </div>
          <div className="ml-auto flex shrink-0 items-center gap-5 text-right">
            <div title={page && !page.q ? "counted in Exasol when this page was read" : "as the last catalogue read reported it"}>
              <div className="num text-xl font-bold leading-none text-fog">{fmt(tableRows)}</div><div className="text-[10.5px] text-mist">rows</div>
            </div>
            <div><div className="num text-xl font-bold leading-none text-fog">{cols.length}</div><div className="text-[10.5px] text-mist">columns</div></div>
            {fq === "GOLDEN.CUSTOMERS" && (
              <div><div className="font-mono text-[13px] font-semibold leading-none text-fog" title={s.golden.fingerprint ?? ""}>{short(s.golden.fingerprint)}</div><div className="text-[10.5px] text-mist">fingerprint</div></div>
            )}
            {t.kind === "TABLE" && !replay && (
              <a href={downloads.table(t.schema, t.table)} title={`Download every row of ${fq} as a CSV file`}
                className="flex items-center gap-1.5 rounded-lg border border-rule px-3 py-1.5 text-[12px] text-fog hover:border-tide hover:text-tide">
                <Download className="h-3.5 w-3.5" /> CSV
              </a>
            )}
          </div>
        </div>
        <nav className="-mb-3.5 mt-3 flex gap-1">
          {([["rows", "Rows", Rows3], ["structure", "Structure", KeyRound]] as const).map(([id, label, Icon]) => (
            <button key={id} onClick={() => setTab(id)}
              className={`relative flex items-center gap-1.5 px-3 pb-2.5 pt-1 text-[12.5px] font-medium ${tab === id ? "text-tide" : "text-mist hover:text-fog"}`}>
              <Icon className="h-3.5 w-3.5" />{label}
              {tab === id && <span className="absolute inset-x-1 bottom-0 h-0.5 rounded bg-tide" />}
            </button>
          ))}
        </nav>
      </header>

      {tab === "structure" ? (
        <Structure fq={fq} cols={cols} keys={keys} kind={t.kind} />
      ) : replay ? (
        <div className="card grid min-h-0 flex-1 place-items-center px-6 text-center">
          <div className="max-w-md">
            <Database className="mx-auto h-7 w-7 text-mist" />
            <p className="mt-3 text-[13px] font-medium text-fog">A replay has no database behind it.</p>
            <p className="mt-1 text-[12.5px] text-mist">
              The tree above is the catalogue this recording carried. Rows are read from the live instance only, so
              nothing is shown here rather than something invented. Go live to read them.
            </p>
          </div>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 gap-3">
          <div className="card flex min-h-0 flex-1 flex-col overflow-hidden">
            <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-rule px-3 py-2">
              <div className="flex items-center gap-2 rounded-lg border border-rule bg-deck/60 px-2.5 py-1.5 focus-within:border-tide/50">
                <Search className="h-3.5 w-3.5 shrink-0 text-mist" />
                <input value={typed} onChange={(e) => setTyped(e.target.value)} placeholder="Filter text columns…"
                  className="w-[190px] bg-transparent text-[12.5px] text-fog outline-none placeholder:text-mist" />
                {typed && <button onClick={() => setTyped("")} className="text-mist hover:text-fog"><X className="h-3.5 w-3.5" /></button>}
              </div>
              {page?.q && (
                <span className="text-[11px] text-mist" title={page.searched.join(", ") || "no character column to search"}>
                  {page.searched.length ? `searching ${page.searched.length} text column${page.searched.length === 1 ? "" : "s"}` : "no text column to search"}
                </span>
              )}
              <div className="ml-auto flex items-center gap-2">
                <label className="flex items-center gap-1.5 text-[11.5px] text-mist">
                  Rows
                  <select value={limit} onChange={(e) => { setLimit(Number(e.target.value)); setOffset(0); }}
                    className="rounded-md border border-rule bg-hull px-1.5 py-1 text-[11.5px] text-fog">
                    {SIZES.map((n) => <option key={n}>{n}</option>)}
                  </select>
                </label>
                <span className="num whitespace-nowrap text-[11.5px] text-mist">{fmt(from)}–{fmt(to)} of {fmt(total)}</span>
                <div className="flex items-center">
                  <button disabled={offset === 0 || busy} onClick={() => setOffset(Math.max(0, offset - limit))}
                    className="rounded-l-md border border-rule px-2 py-1 text-[11.5px] text-fog disabled:opacity-40 hover:enabled:bg-deck">Prev</button>
                  <button disabled={last || busy} onClick={() => setOffset(offset + limit)}
                    className="-ml-px rounded-r-md border border-rule px-2 py-1 text-[11.5px] text-fog disabled:opacity-40 hover:enabled:bg-deck">Next</button>
                </div>
                <button onClick={() => setNonce((n) => n + 1)} disabled={busy} title="Read this page from Exasol again"
                  className="rounded-md border border-rule px-2 py-1 text-mist hover:text-fog disabled:opacity-40">
                  <RefreshCw className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
                </button>
              </div>
            </div>

            <Grid page={page} cols={cols} keys={keys} err={err} busy={busy} offset={offset} q={q}
              sort={sort} onSort={clickCol} row={row} onRow={setRow} />

            <footer className="shrink-0 border-t border-rule px-3 py-1.5 text-[11px] text-mist">
              {page ? (
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                  <span>read from Exasol in {page.ms} ms at {page.db_time.slice(11, 19)}</span>
                  <span>·</span>
                  <span title={page.order_implicit ? "No sort was chosen, so the first column orders the page — paging is not stable without an ORDER BY." : undefined}>
                    ordered by {page.order} {page.dir.toLowerCase()}{page.order_implicit ? " (default)" : ""}
                  </span>
                  <button onClick={() => setShowSql((v) => !v)} className="ml-auto flex items-center gap-1 text-mist hover:text-tide">
                    <Terminal className="h-3 w-3" />{showSql ? "hide" : "show"} the statement
                  </button>
                </div>
              ) : <span>&nbsp;</span>}
              {showSql && page && (
                <pre className="scroll-thin mt-1.5 max-h-24 overflow-auto whitespace-pre-wrap rounded-lg bg-deck/70 px-2.5 py-2 font-mono text-[11px] leading-relaxed text-fog/85">{page.sql}</pre>
              )}
            </footer>
          </div>

          {row != null && page?.rows[row] && (
            <RowCard cols={cols} values={page.rows[row]} keys={keys} n={offset + row + 1} onClose={() => setRow(null)}
              onHistory={fq === "GOLDEN.CUSTOMERS" ? onHistory : undefined} />
          )}
        </div>
      )}
    </section>
  );
}

// ------------------------------------------------------------------ the grid

function Grid({ page, cols, keys, err, busy, offset, q, sort, onSort, row, onRow }: {
  page: Page | null; cols: [string, string][]; keys: Record<string, string>; err: string | null; busy: boolean;
  offset: number; q: string; sort: { col: string; dir: "ASC" | "DESC" } | null; onSort: (c: string) => void;
  row: number | null; onRow: (i: number | null) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const rows = page?.rows ?? [];

  const key = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const next = row == null ? 0 : Math.min(rows.length - 1, Math.max(0, row + (e.key === "ArrowDown" ? 1 : -1)));
    onRow(next);
    box.current?.querySelector<HTMLElement>(`[data-r="${next}"]`)?.scrollIntoView({ block: "nearest" });
  };

  if (err) {
    return (
      <div className="grid min-h-0 flex-1 place-items-center px-6 text-center">
        <div className="max-w-md">
          <p className="text-[13px] font-medium text-flare">Exasol did not return this page.</p>
          <p className="mt-1 font-mono text-[12px] text-mist">{err}</p>
        </div>
      </div>
    );
  }
  if (!page && busy) return <div className="grid min-h-0 flex-1 place-items-center text-[12.5px] text-mist">Reading…</div>;
  if (page && rows.length === 0) {
    return (
      <div className="grid min-h-0 flex-1 place-items-center px-6 text-center text-[12.5px] text-mist">
        {q ? `No row matches “${q}”.` : "This table is empty."}
      </div>
    );
  }

  return (
    <div ref={box} tabIndex={0} onKeyDown={key}
      className={`scroll-thin min-h-0 flex-1 overflow-auto outline-none transition-opacity ${busy ? "opacity-60" : ""}`}>
      <table className="w-full border-separate border-spacing-0 text-[12px]">
        <thead>
          <tr>
            <th className="sticky left-0 top-0 z-20 border-b border-r border-rule bg-deck px-2 py-1.5 text-right font-medium text-mist">#</th>
            {cols.map(([c, typ]) => {
              const k = keys[c];
              const on = sort?.col === c;
              return (
                <th key={c} className="sticky top-0 z-10 whitespace-nowrap border-b border-rule bg-deck px-2.5 py-1 text-left">
                  <button onClick={() => onSort(c)} title={`${c} · ${typeLabel(typ)}${k ? ` · ${k.toLowerCase()}` : ""}\nClick to sort`}
                    className="flex w-full items-center gap-1 leading-tight">
                    {k && <KeyRound className={`h-3 w-3 shrink-0 ${k.startsWith("PRIMARY KEY") ? "text-brass" : "text-tide"}`} />}
                    <span className={`font-mono text-[11.5px] ${on ? "font-semibold text-tide" : "font-medium text-fog"}`}>{c}</span>
                    {on && (sort!.dir === "ASC" ? <ArrowUp className="h-3 w-3 text-tide" /> : <ArrowDown className="h-3 w-3 text-tide" />)}
                    <span className="ml-auto pl-2 font-mono text-[10px] font-normal text-mist">{typeLabel(typ)}</span>
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => {
            const on = row === i;
            return (
              <tr key={i} data-r={i} onClick={() => onRow(on ? null : i)}
                className={`cursor-default ${on ? "bg-tide/5" : "hover:bg-deck/50"}`}>
                <td className={`num sticky left-0 z-10 border-b border-r border-rule px-2 py-[5px] text-right align-top text-[11px] tabular-nums
                  ${on ? "bg-tide/10 font-semibold text-tide" : "bg-hull text-mist"}`}>{offset + i + 1}</td>
                {r.map((v, j) => <Cell key={j} v={v} type={cols[j]?.[1] ?? ""} q={q} />)}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Cell({ v, type, q }: { v: string | number | boolean | null; type: string; q: string }) {
  const base = "border-b border-rule/70 px-2.5 py-[5px] align-top";
  if (v === null) return <td className={`${base} text-[11.5px] italic text-mist/70`}>NULL</td>;
  if (typeof v === "boolean") return <td className={`${base} font-mono text-[11.5px] text-fog/80`}>{v ? "TRUE" : "FALSE"}</td>;
  // A stored value is shown as stored — no thousands separators on what may be an id.
  if (NUMERIC.test(type) || typeof v === "number") return <td className={`${base} num whitespace-nowrap text-right text-fog`}>{String(v)}</td>;
  const text = String(v);
  return (
    <td className={`${base} ${TEMPORAL.test(type) ? "whitespace-nowrap font-mono text-[11.5px] text-fog/85" : "text-fog"}`} title={text.length > 48 ? text : undefined}>
      <span className="block max-w-[320px] truncate">{mark(text, q)}</span>
    </td>
  );
}

/** The filter's match, highlighted exactly where the LIKE found it. */
function mark(text: string, q: string) {
  const needle = q.trim();
  if (!needle) return text;
  const i = text.toUpperCase().indexOf(needle.toUpperCase());
  if (i < 0) return text;
  return (<>{text.slice(0, i)}<mark className="rounded-sm bg-brass/20 px-px text-fog">{text.slice(i, i + needle.length)}</mark>{text.slice(i + needle.length)}</>);
}

// ------------------------------------------------------------------ one row, read downwards

function RowCard({ cols, values, keys, n, onClose, onHistory }: {
  cols: [string, string][]; values: (string | number | boolean | null)[]; keys: Record<string, string>; n: number; onClose: () => void;
  onHistory?: (goldenId: string) => void;
}) {
  const gid = values[cols.findIndex(([c]) => c === "GOLDEN_ID")];
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    const obj = Object.fromEntries(cols.map(([c], i) => [c, values[i]]));
    try {
      await navigator.clipboard.writeText(JSON.stringify(obj, null, 2));
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch { /* a browser that refuses the clipboard is not an error worth showing */ }
  };
  return (
    <aside className="card flex w-[320px] shrink-0 flex-col overflow-hidden">
      <header className="flex items-center gap-2 border-b border-rule px-3 py-2">
        <span className="num text-[12.5px] font-semibold text-fog">Row {fmt(n)}</span>
        <button onClick={copy} title="Copy this row as JSON" className="ml-auto flex items-center gap-1 text-[11px] text-mist hover:text-tide">
          <Copy className="h-3.5 w-3.5" />{copied ? "copied" : "copy"}
        </button>
        <button onClick={onClose} className="text-mist hover:text-fog"><X className="h-4 w-4" /></button>
      </header>
      {onHistory && gid != null && (
        <button onClick={() => onHistory(String(gid))}
          className="mx-3 mt-2.5 flex items-center justify-center gap-1.5 rounded-lg border border-tide/40 bg-tide/5 px-3 py-2 text-[12.5px] font-semibold text-tide hover:bg-tide/10">
          <History className="h-4 w-4" /> Show this customer's history
        </button>
      )}
      <dl className="scroll-thin min-h-0 flex-1 divide-y divide-rule/70 overflow-y-auto">
        {cols.map(([c, typ], i) => {
          const v = values[i];
          return (
            <div key={c} className="px-3 py-1.5">
              <dt className="flex items-center gap-1.5">
                {keys[c] && <KeyRound className={`h-3 w-3 shrink-0 ${keys[c].startsWith("PRIMARY KEY") ? "text-brass" : "text-tide"}`} />}
                <span className="font-mono text-[11px] font-medium text-fog/85">{c}</span>
                <span className="ml-auto font-mono text-[10px] text-mist">{typeLabel(typ)}</span>
              </dt>
              <dd className={`mt-0.5 break-words text-[12.5px] ${v === null ? "italic text-mist/70" : "text-fog"}`}>
                {v === null ? "NULL" : typeof v === "boolean" ? (v ? "TRUE" : "FALSE") : String(v)}
              </dd>
            </div>
          );
        })}
      </dl>
    </aside>
  );
}

// ------------------------------------------------------------------ structure

function Structure({ fq, cols, keys, kind }: { fq: string; cols: [string, string][]; keys: Record<string, string>; kind: string }) {
  const pad = Math.max(0, ...cols.map(([c]) => c.length));
  const ddl = [`CREATE ${kind} ${fq} (`,
    ...cols.map(([c, t], i) => `    ${c.padEnd(pad)}  ${t}${i < cols.length - 1 ? "," : ""}`),
    ");",
    ...Object.entries(keys).map(([c, k]) => `-- ${k} (${c})`)].join("\n");

  return (
    <div className="scroll-thin grid min-h-0 flex-1 grid-cols-2 gap-4 overflow-y-auto pr-0.5">
      <section className="card flex min-h-0 flex-col overflow-hidden">
        <header className="border-b border-rule px-4 py-2.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-mist">
          Columns · {cols.length}
        </header>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
          <table className="w-full text-[12px]">
            <thead className="sticky top-0 bg-deck/80 text-left text-[10.5px] uppercase tracking-wide text-mist">
              <tr><th className="w-8 px-3 py-1.5 font-medium">#</th><th className="font-medium">Column</th><th className="font-medium">Type</th><th className="pr-3 font-medium">Key</th></tr>
            </thead>
            <tbody>
              {cols.length === 0 && <tr><td colSpan={4} className="px-3 py-6 text-mist">Exasol reports no columns for this object.</td></tr>}
              {cols.map(([c, t], i) => (
                <tr key={c} className="border-t border-rule/70">
                  <td className="num px-3 py-1.5 text-right text-mist">{i + 1}</td>
                  <td className="font-mono text-fog">{c}</td>
                  <td className="font-mono text-[11px] text-mist">{typeLabel(t)}</td>
                  <td className="pr-3">
                    {keys[c] && (
                      <span className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10.5px] font-medium
                        ${keys[c].startsWith("PRIMARY KEY") ? "bg-brass/10 text-brass" : "bg-tide/10 text-tide"}`}>
                        <KeyRound className="h-3 w-3" />{keys[c].toLowerCase()}
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card flex min-h-0 flex-col overflow-hidden">
        <header className="border-b border-rule px-4 py-2.5 text-[11px] font-semibold uppercase tracking-[0.14em] text-mist">
          How it is declared
        </header>
        <div className="scroll-thin min-h-0 flex-1 space-y-3 overflow-y-auto px-4 py-3">
          <pre className="scroll-thin overflow-x-auto rounded-xl bg-deck/70 px-3 py-2.5 font-mono text-[11.5px] leading-relaxed text-fog/85">{ddl}</pre>
          <p className="text-[11.5px] leading-relaxed text-mist">
            Rebuilt from the columns and constraints Exasol reports for {fq} — it is not the stored {kind === "VIEW" ? "view definition" : "DDL text"}, and
            it carries no defaults or comments the database was not asked for.
          </p>
          {Object.keys(keys).length === 0 && (
            <p className="text-[11.5px] leading-relaxed text-mist">
              Exasol reports no declared key here. Where Drydock needs one to diff a table it registers it in
              DRYDOCK.TABLE_KEYS and verifies it is unique first.
            </p>
          )}
        </div>
      </section>
    </div>
  );
}
