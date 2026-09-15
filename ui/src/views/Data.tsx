// Your data: what Drydock is working on, whether it is ready to run, and the four steps to bring your own two
// customer files. Upload -> match columns -> check -> load. Nothing is loaded until the person has seen the
// quality report and pressed Load; the uploaded copies are deleted once the data is in Exasol.
//
// The dataset and the readiness checklist come from db.snapshot on the event stream; the wizard is a form, and
// talks to the orchestrator's /uploads endpoints (REST, like every reviewer action).

import {
  AlertTriangle, ArrowRight, Check, CheckCircle2, CircleAlert, CircleDashed, Database, Download, FileSpreadsheet,
  Info, Loader2, Play, RefreshCw, Sparkles, Upload, X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, downloads } from "../api";
import { checkWord, explainError } from "../derive";
import type { DatasetInfo, FileProfile, QualityReport, Readiness, SideMapping, SideReport, State } from "../types";
import { fmt, reportError, toast } from "../ui";

type Side = "a" | "b";
const SIDE_TITLE: Record<Side, string> = { a: "System A", b: "System B" };
const SIDE_HELP: Record<Side, string> = {
  a: "The list you trust most, such as your main CRM. The clean list starts from it.",
  b: "The list you want to fold in, such as a shop, a newly acquired company, or a sign-up form.",
};
const DATE_FIELDS = ["dob", "updated"];

export function DataView({ s, canAct, onGoRun, onTour, onLoaded }: {
  s: State; canAct: boolean; onGoRun: () => void; onTour: () => void; onLoaded: () => void;
}) {
  const ds = s.system.snapshot?.config.dataset;
  const ready = s.system.snapshot?.config.readiness;
  const [profiles, setProfiles] = useState<Record<Side, FileProfile | null>>({ a: null, b: null });
  const [maps, setMaps] = useState<Record<Side, SideMapping>>({ a: { fields: {}, formats: {} }, b: { fields: {}, formats: {} } });
  const [report, setReport] = useState<QualityReport | null>(null);
  const [label, setLabel] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState<{ rows: number; seconds: number } | null>(null);

  const adopt = (side: Side, p: FileProfile | null) => {
    setProfiles((x) => ({ ...x, [side]: p }));
    setReport(null);
    setDone(null);
    if (p) setMaps((m) => ({ ...m, [side]: { fields: { ...p.suggested }, formats: guessFormats(p, p.suggested) } }));
  };

  useEffect(() => {                      // pick up files uploaded earlier (a reload, or another tab)
    api.uploaded().then((u) => { adopt("a", u.a); adopt("b", u.b); }).catch(() => undefined);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const upload = async (side: Side, file: File) => {
    setBusy(`upload-${side}`);
    try {
      adopt(side, await api.uploadFile(side, file));
      toast("good", `${SIDE_TITLE[side]} uploaded`, `${file.name}: now check how its columns match up.`);
    } catch (e) { reportError(e); }
    setBusy(null);
  };

  const setField = (side: Side, key: string, col: string | null) => {
    setReport(null);
    setMaps((m) => {
      const fields = { ...m[side].fields, [key]: col };
      const formats = { ...m[side].formats };
      if (DATE_FIELDS.includes(key)) formats[key] = col ? profiles[side]?.columns.find((c) => c.name === col)?.date?.format ?? null : null;
      return { ...m, [side]: { fields, formats } };
    });
  };
  const setFormat = (side: Side, key: string, f: string) => {
    setReport(null);
    setMaps((m) => ({ ...m, [side]: { ...m[side], formats: { ...m[side].formats, [key]: f } } }));
  };

  const check = async () => {
    setBusy("check");
    try { setReport(await api.checkUploads(maps.a, maps.b)); } catch (e) { reportError(e); }
    setBusy(null);
  };
  const load = async () => {
    setBusy("load");
    try {
      const out = await api.loadUploads(maps.a, maps.b, label || `${profiles.a?.filename} + ${profiles.b?.filename}`);
      setDone({ rows: out.golden_rows, seconds: out.seconds });
      setProfiles({ a: null, b: null });
      setReport(null);
      toast("good", "Your data is loaded", `The clean list now starts from your ${fmt(out.rows_a)} System A customers.`);
      onLoaded();
    } catch (e: any) {
      if (e?.body?.report) setReport(e.body.report);
      reportError(e);
    }
    setBusy(null);
  };
  const demo = async () => {
    if (!confirm("Switch back to the demo data? Drydock's current runs, reviews and undo history are cleared.")) return;
    setBusy("demo");
    try { await api.useDemo(); setDone(null); toast("good", "Back on the demo data"); } catch (e) { reportError(e); }
    setBusy(null);
  };

  const both = !!(profiles.a && profiles.b);
  const nameOk = (side: Side) => !!(maps[side].fields.full_name || maps[side].fields.first_name || maps[side].fields.last_name);
  const mapped = both && nameOk("a") && nameOk("b");

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-3">
        <FileSpreadsheet className="mt-1 h-6 w-6 text-navy" />
        <div>
          <h1 className="text-2xl font-bold text-fog">Your data</h1>
          <p className="text-[13px] text-mist">
            Bring the two customer lists you want to reconcile. Drydock checks them and shows you what it found, and only loads
            them when you say so.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-[1fr_1fr] gap-4">
        <NowCard ds={ds} onDemo={demo} busy={busy === "demo"} canAct={canAct} />
        <ReadyCard ready={ready} connected={!!s.system.snapshot} version={s.system.snapshot?.version} ds={ds} />
      </div>

      {done && (
        <div className="card flex items-center gap-4 border-kelp/40 bg-kelp/5 px-5 py-4">
          <CheckCircle2 className="h-8 w-8 shrink-0 text-kelp" />
          <div className="min-w-0">
            <div className="text-[15px] font-bold text-fog">Your data is ready: the clean list starts with {fmt(done.rows)} customers</div>
            <div className="text-[13px] text-fog/80">Next, start a run: Drydock finds the duplicates and asks you before it changes anything.</div>
          </div>
          <div className="ml-auto flex shrink-0 gap-2">
            <button onClick={onTour} className="flex items-center gap-2 rounded-lg border border-rule bg-hull px-4 py-2 text-[13px] font-medium"><Sparkles className="h-4 w-4 text-tide" /> Show me around</button>
            <button onClick={onGoRun} className="flex items-center gap-2 rounded-lg bg-navy px-4 py-2 text-[13px] font-semibold text-white"><Play className="h-4 w-4 fill-white" /> Start a run</button>
          </div>
        </div>
      )}

      <section className="card px-5 py-4" data-tour="data-wizard">
        <div className="flex items-center gap-3">
          <Upload className="h-5 w-5 text-navy" />
          <h2 className="text-lg font-bold text-fog">Use your own data</h2>
          <span className="text-[12.5px] text-mist">Two CSV files · up to 50 MB and 300,000 rows each</span>
          <div className="ml-auto flex items-center gap-2 text-[12.5px] text-mist">
            No files to hand? Try ours:
            <a href={downloads.sampleA} className="flex items-center gap-1 font-medium text-tide hover:underline"><Download className="h-3.5 w-3.5" />System A sample</a>
            <a href={downloads.sampleB} className="flex items-center gap-1 font-medium text-tide hover:underline"><Download className="h-3.5 w-3.5" />System B sample</a>
          </div>
        </div>

        <Stepline n={1} title="Upload both files" done={both}>
          <div className="grid grid-cols-2 gap-4">
            {(["a", "b"] as Side[]).map((side) => (
              <Drop key={side} side={side} profile={profiles[side]} busy={busy === `upload-${side}`} disabled={!canAct}
                onFile={(f) => upload(side, f)} onClear={() => { api.forgetUpload(side).catch(() => undefined); adopt(side, null); }} />
            ))}
          </div>
        </Stepline>

        <Stepline n={2} title="Tell Drydock which column is which" done={mapped} disabled={!both}
          hint="We guessed from the column names and the values. Check each one; leave anything your file doesn't have as 'not in this file'.">
          {both && (
            <div className="grid grid-cols-2 gap-4">
              {(["a", "b"] as Side[]).map((side) => (
                <MappingTable key={side} side={side} profile={profiles[side]!} map={maps[side]}
                  onField={(k, c) => setField(side, k, c)} onFormat={(k, f) => setFormat(side, k, f)} />
              ))}
            </div>
          )}
        </Stepline>

        <Stepline n={3} title="Check the data" done={!!report} disabled={!mapped}
          hint="Drydock reads every row with your choices and tells you what it found. Nothing is loaded yet.">
          {mapped && (
            <div className="space-y-3">
              <button onClick={check} disabled={busy !== null || !canAct} data-tour="data-check"
                className="flex items-center gap-2 rounded-lg bg-navy px-5 py-2.5 text-[13.5px] font-semibold text-white disabled:opacity-40">
                {busy === "check" ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
                {report ? "Check again" : "Check my data"}
              </button>
              {report && <ReportView report={report} />}
            </div>
          )}
        </Stepline>

        <Stepline n={4} title="Load it" done={!!done} disabled={!report} last>
          {report && (
            <div className="space-y-3">
              <div className="flex items-start gap-2 rounded-lg border border-brass/30 bg-brass/5 px-3 py-2 text-[12.5px] text-fog">
                <Info className="mt-0.5 h-4 w-4 shrink-0 text-brass" />
                <span>Loading replaces what Drydock is working on now: its runs, reviews and undo history start again, on your data.
                  Your two files are copied into Exasol, and the uploaded copies are then deleted from the server.</span>
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <label className="flex items-center gap-2 text-[13px]">Name this dataset
                  <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder={`${profiles.a?.filename} + ${profiles.b?.filename}`}
                    className="w-[320px] rounded-lg border border-rule px-3 py-2 text-[13px] outline-none focus:border-tide" />
                </label>
                <button onClick={load} disabled={!report.can_load || busy !== null || !canAct} data-tour="data-load"
                  className="flex items-center gap-2 rounded-lg bg-kelp px-5 py-2.5 text-[13.5px] font-semibold text-white disabled:opacity-40">
                  {busy === "load" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Database className="h-4 w-4" />}
                  {busy === "load" ? "Loading…" : "Load my data"}
                </button>
                {!report.can_load && <span className="text-[12.5px] text-flare">Fix the problems marked in red above first.</span>}
              </div>
            </div>
          )}
        </Stepline>
      </section>
    </div>
  );
}

// ------------------------------------------------------------------ status cards

function NowCard({ ds, onDemo, busy, canAct }: { ds?: DatasetInfo; onDemo: () => void; busy: boolean; canAct: boolean }) {
  return (
    <section className="card px-5 py-4" data-tour="data-now">
      <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-mist">Now working on</div>
      {!ds ? <div className="mt-2 text-[13px] text-mist">Waiting for the server…</div> : (
        <>
          <div className="mt-1 flex items-center gap-2">
            <span className="truncate text-[17px] font-bold text-fog">{ds.label}</span>
            <span className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-semibold ${ds.name === "demo" ? "bg-sky/10 text-sky" : "bg-kelp/10 text-kelp"}`}>
              {ds.name === "demo" ? "demo" : "your files"}
            </span>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <Stat label="System A" value={ds.rows_a} note={ds.files?.a ?? ds.tables.a} />
            <Stat label="System B" value={ds.rows_b} note={ds.files?.b ?? ds.tables.b} />
          </div>
          <div className="mt-3 text-[12px] leading-snug text-mist">
            {ds.scored
              ? "The demo comes with an answer key, so every run is scored against the truth."
              : "Your data has no answer key, so runs are not scored: your review is the check."}
            {ds.activated_at && <> Loaded {new Date(ds.activated_at).toLocaleString("en-GB")}.</>}
          </div>
          {ds.name !== "demo" && (
            <button onClick={onDemo} disabled={busy || !canAct}
              className="mt-3 rounded-lg border border-rule px-3 py-1.5 text-[12.5px] text-fog hover:border-tide disabled:opacity-40">
              {busy ? "Switching…" : "Use the demo data instead"}
            </button>
          )}
        </>
      )}
    </section>
  );
}

function Stat({ label, value, note }: { label: string; value: number; note?: string | null }) {
  return (
    <div className="rounded-lg bg-deck/60 px-3 py-2">
      <div className="text-[11.5px] text-mist">{label}</div>
      <div className="num text-xl font-bold text-fog">{fmt(value)}</div>
      {note && <div className="truncate text-[11px] text-mist" title={note}>{note}</div>}
    </div>
  );
}

function ReadyCard({ ready, connected, version, ds }: { ready?: Readiness; connected: boolean; version?: string; ds?: DatasetInfo }) {
  const fix = explainError("NotVerified");
  const rows: { ok: boolean | null; title: string; detail: React.ReactNode }[] = [
    { ok: connected, title: "Connected to the database", detail: connected ? `Exasol ${version} is answering.` : "Waiting for the server to report in." },
    {
      ok: ready ? ready.checks_ok : null, title: "Safety checks signed",
      detail: !ready ? "…" : ready.checks_ok ? "Drydock has proof the database behaves the way it relies on."
        : <>{groupChecks(ready.checks)}.
            <span className="mt-1 block rounded-md bg-deck px-2 py-1 text-fog"><b>How to fix: </b>{fix.fix}</span></>,
    },
    { ok: ds ? ds.rows_a > 0 && ds.rows_b > 0 : null, title: "Data loaded", detail: ds ? `${fmt(ds.rows_a)} + ${fmt(ds.rows_b)} records` : "…" },
    {
      ok: ready ? (ready.gemini_key ? true : null) : null, title: "AI helper (optional)",
      detail: ready?.gemini_key ? "Gemini is set up: it advises on the pairs the matchers disagree on."
        : "No Gemini key: pairs the matchers disagree on simply wait for you.",
    },
  ];
  return (
    <section className="card px-5 py-4" data-tour="data-ready">
      <div className="text-[11px] font-semibold uppercase tracking-[0.14em] text-mist">Ready to run?</div>
      <ul className="mt-2 space-y-2">
        {rows.map((r) => (
          <li key={r.title} className="flex items-start gap-2.5">
            {r.ok === true ? <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-kelp" />
              : r.ok === false ? <CircleAlert className="mt-0.5 h-4 w-4 shrink-0 text-flare" />
              : <CircleDashed className="mt-0.5 h-4 w-4 shrink-0 text-mist" />}
            <div className="min-w-0 text-[12.5px] leading-snug">
              <div className="font-semibold text-fog">{r.title}</div>
              <div className="text-fog/75">{r.detail}</div>
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** "V3, V4 and V5 cannot be read: this server has no secret word" rather than the same words five times. */
function groupChecks(checks: Record<string, string>): string {
  const by: Record<string, string[]> = {};
  for (const [k, v] of Object.entries(checks)) if (v !== "PASS") (by[v] ??= []).push(k);
  const list = (ks: string[]) => (ks.length > 1 ? `${ks.slice(0, -1).join(", ")} and ${ks.at(-1)}` : ks[0]);
  return Object.entries(by).map(([v, ks]) => `${list(ks)} ${checkWord(v)}`).join("; ");
}

// ------------------------------------------------------------------ wizard pieces

function Stepline({ n, title, done, disabled, hint, last, children }: {
  n: number; title: string; done: boolean; disabled?: boolean; hint?: string; last?: boolean; children?: React.ReactNode;
}) {
  return (
    <div className={`relative mt-4 pl-11 ${disabled ? "opacity-45" : ""}`}>
      <span className={`absolute left-0 top-0 grid h-8 w-8 place-items-center rounded-full text-[13px] font-bold
        ${done ? "bg-kelp text-white" : disabled ? "bg-deck text-mist" : "bg-tide text-white"}`}>{done ? <Check className="h-4 w-4" /> : n}</span>
      {!last && <span className="absolute bottom-[-12px] left-[15px] top-9 w-px bg-rule" />}
      <div className="text-[14.5px] font-semibold text-fog">{title}</div>
      {hint && !disabled && <div className="text-[12.5px] text-mist">{hint}</div>}
      {!disabled && children && <div className="mt-2.5">{children}</div>}
    </div>
  );
}

function Drop({ side, profile, busy, disabled, onFile, onClear }: {
  side: Side; profile: FileProfile | null; busy: boolean; disabled: boolean; onFile: (f: File) => void; onClear: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  if (profile) {
    return (
      <div className="rounded-xl border border-kelp/40 bg-kelp/5 px-4 py-3">
        <div className="flex items-center gap-2">
          <CheckCircle2 className="h-4 w-4 text-kelp" />
          <span className="text-[13px] font-semibold text-fog">{SIDE_TITLE[side]}</span>
          <span className="truncate text-[12.5px] text-fog/80">{profile.filename}</span>
          <button onClick={onClear} className="ml-auto text-[12px] text-mist hover:text-flare" title="Remove and choose another file">replace</button>
        </div>
        <div className="mt-1 text-[12px] text-mist">
          {fmt(profile.rows)} rows · {profile.columns.length} columns · {profile.encoding}, {profile.delimiter}-separated
          {profile.ragged > 0 && <span className="text-brass"> · {fmt(profile.ragged)} rows had a different number of columns</span>}
        </div>
        <div className="scroll-thin mt-2 overflow-x-auto rounded-md border border-rule bg-hull">
          <table className="text-[11.5px]">
            <thead className="bg-deck text-left text-mist"><tr>{profile.columns.slice(0, 8).map((c) => <th key={c.name} className="whitespace-nowrap px-2 py-1 font-medium">{c.name}</th>)}</tr></thead>
            <tbody>{profile.preview.slice(0, 3).map((r, i) => (
              <tr key={i} className="border-t border-rule">{r.slice(0, 8).map((v, j) => <td key={j} className="max-w-[140px] truncate whitespace-nowrap px-2 py-1">{v}</td>)}</tr>
            ))}</tbody>
          </table>
        </div>
      </div>
    );
  }
  return (
    <div onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); const f = e.dataTransfer.files[0]; if (f && !disabled) onFile(f); }}
      onClick={() => !disabled && input.current?.click()} role="button" data-tour={`drop-${side}`}
      className={`flex cursor-pointer flex-col items-center justify-center gap-1.5 rounded-xl border-2 border-dashed px-4 py-6 text-center transition-colors
        ${over ? "border-tide bg-tide/5" : "border-rule hover:border-tide/60"} ${disabled ? "cursor-not-allowed opacity-50" : ""}`}>
      {busy ? <Loader2 className="h-7 w-7 animate-spin text-tide" /> : <Upload className="h-7 w-7 text-tide" />}
      <div className="text-[14px] font-semibold text-fog">{SIDE_TITLE[side]}</div>
      <div className="max-w-[320px] text-[12px] text-mist">{SIDE_HELP[side]}</div>
      <div className="mt-1 text-[12.5px] font-medium text-tide">Drop a CSV file here, or click to choose</div>
      <div className="text-[11px] text-mist">From Excel: File › Save As › CSV UTF-8</div>
      <input ref={input} type="file" accept=".csv,.txt,.tsv,text/csv" className="hidden"
        onChange={(e) => { const f = e.target.files?.[0]; if (f) onFile(f); e.target.value = ""; }} />
    </div>
  );
}

function MappingTable({ side, profile, map, onField, onFormat }: {
  side: Side; profile: FileProfile; map: SideMapping; onField: (k: string, c: string | null) => void; onFormat: (k: string, f: string) => void;
}) {
  const byName = useMemo(() => Object.fromEntries(profile.columns.map((c) => [c.name, c])), [profile]);
  const nameOk = !!(map.fields.full_name || map.fields.first_name || map.fields.last_name);
  return (
    <div className="rounded-xl border border-rule">
      <div className="flex items-center gap-2 border-b border-rule bg-deck/60 px-3 py-2">
        <span className="text-[13px] font-semibold text-fog">{SIDE_TITLE[side]}</span>
        <span className="truncate text-[12px] text-mist">{profile.filename}</span>
      </div>
      {!nameOk && (
        <div className="flex items-center gap-2 border-b border-rule bg-flare/5 px-3 py-1.5 text-[12px] text-flare">
          <AlertTriangle className="h-3.5 w-3.5" /> Choose the column with the customer's name (full name, or first and last).
        </div>
      )}
      <table className="w-full text-[12.5px]">
        <tbody>
          {profile.fields.map((f) => {
            const col = map.fields[f.key] ?? null;
            const c = col ? byName[col] : null;
            const isDate = DATE_FIELDS.includes(f.key);
            return (
              <tr key={f.key} className="border-t border-rule/70 first:border-t-0 align-top">
                <td className="w-[172px] px-3 py-1.5" title={f.help}>
                  <div className="font-medium text-fog">{f.label}</div>
                  {f.help && <div className="text-[11px] leading-tight text-mist">{f.help.split(/(?<=\.) /)[0]}</div>}
                </td>
                <td className="px-2 py-1.5">
                  <select value={col ?? ""} onChange={(e) => onField(f.key, e.target.value || null)}
                    className={`w-full rounded-md border px-2 py-1 text-[12.5px] ${col ? "border-tide/40 bg-tide/5" : "border-rule bg-hull text-mist"}`}>
                    <option value="">— not in this file —</option>
                    {profile.columns.map((pc) => <option key={pc.name} value={pc.name}>{pc.name}</option>)}
                  </select>
                  {c && <div className="mt-0.5 truncate text-[11px] text-mist" title={c.samples.join(" · ")}>e.g. {c.samples.slice(0, 2).join(" · ") || "(empty)"} · {Math.round((100 * c.filled) / Math.max(1, profile.rows))}% filled</div>}
                  {isDate && col && (
                    <DateChoice samples={c?.samples ?? []} guess={c?.date ?? null} value={map.formats[f.key] ?? null} onChange={(v) => onFormat(f.key, v)} />
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const ALL_FORMATS: { format: string; label: string }[] = [
  { format: "%Y-%m-%d", label: "YYYY-MM-DD" }, { format: "%d/%m/%Y", label: "DD/MM/YYYY (day first)" },
  { format: "%m/%d/%Y", label: "MM/DD/YYYY (month first)" }, { format: "%d.%m.%Y", label: "DD.MM.YYYY" },
  { format: "%d-%m-%Y", label: "DD-MM-YYYY" }, { format: "%Y/%m/%d", label: "YYYY/MM/DD" },
  { format: "%d/%m/%y", label: "DD/MM/YY (day first)" }, { format: "%m/%d/%y", label: "MM/DD/YY (month first)" },
  { format: "%d %b %Y", label: "3 Apr 1985" }, { format: "%Y%m%d", label: "YYYYMMDD" },
];

function DateChoice({ samples, guess, value, onChange }: {
  samples: string[]; guess: { format: string; ambiguous: boolean; options: { format: string; label: string }[] } | null;
  value: string | null; onChange: (f: string) => void;
}) {
  const options = guess?.ambiguous ? guess.options : ALL_FORMATS;
  const sample = samples.find(Boolean);
  const shown = value ? readDate(sample, value) : null;
  return (
    <div className={`mt-1 rounded-md px-2 py-1 ${guess?.ambiguous || !guess ? "bg-brass/10" : "bg-deck/60"}`}>
      <div className="text-[11px] font-medium text-fog">
        {guess?.ambiguous ? "Which way round are these dates?" : !guess ? "Which format are these dates in?" : "Date format"}
      </div>
      <select value={value ?? ""} onChange={(e) => onChange(e.target.value)} className="mt-0.5 w-full rounded border border-rule bg-hull px-1.5 py-0.5 text-[12px]">
        {!value && <option value="">choose…</option>}
        {options.map((o) => <option key={o.format} value={o.format}>{o.label}</option>)}
      </select>
      {sample && shown && <div className="mt-0.5 text-[11px] text-mist">"{sample}" reads as {shown}</div>}
      {sample && value && !shown && <div className="mt-0.5 text-[11px] text-flare">"{sample}" does not fit this format</div>}
    </div>
  );
}

/** "03/04/1985" with "%d/%m/%Y" -> "3 April 1985": enough to let a person see which way round a format reads. */
function readDate(s: string | undefined, fmt: string): string | null {
  if (!s) return null;
  const v = s.trim().split(/[ T](?=\d{1,2}:\d{2})/)[0];
  const order: string[] = [];
  const rx = fmt.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&").replace(/%([YymdbB])/g, (_m, t) => {
    order.push(t);
    return t === "Y" ? "(\\d{4})" : t === "y" ? "(\\d{2})" : t === "b" || t === "B" ? "([A-Za-z]+)" : "(\\d{1,2})";
  });
  const m = new RegExp(`^${rx}$`).exec(v);
  if (!m) return null;
  let y = 0, mo = 0, d = 0;
  order.forEach((t, i) => {
    const x = m[i + 1];
    if (t === "Y") y = +x;
    else if (t === "y") y = 2000 + +x > new Date().getFullYear() ? 1900 + +x : 2000 + +x;
    else if (t === "m") mo = +x;
    else if (t === "d") d = +x;
    else mo = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].indexOf(x.slice(0, 3).toLowerCase()) + 1;
  });
  const dt = new Date(y, mo - 1, d);
  if (!y || !mo || !d || dt.getMonth() !== mo - 1 || dt.getDate() !== d) return null;
  return dt.toLocaleDateString("en-GB", { day: "numeric", month: "long", year: "numeric" });
}

function guessFormats(p: FileProfile, fields: Record<string, string | null>): Record<string, string | null> {
  const out: Record<string, string | null> = {};
  for (const k of DATE_FIELDS) {
    const col = fields[k];
    const g = col ? p.columns.find((c) => c.name === col)?.date : null;
    out[k] = g ? g.format : null;
  }
  return out;
}

// ------------------------------------------------------------------ the quality report

function ReportView({ report }: { report: QualityReport }) {
  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-4">
        <SideReportCard r={report.a} title="System A" />
        <SideReportCard r={report.b} title="System B" />
      </div>
      <div className="flex items-start gap-2 rounded-lg border border-tide/30 bg-tide/5 px-3 py-2 text-[12.5px] text-fog">
        <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 text-tide" />
        <span>A first look across both files: <b>{fmt(report.overlap.shared_emails)}</b> email addresses and <b>{fmt(report.overlap.shared_phones)}</b> phone
          numbers appear in both, so those customers are probably in both lists. A run will find these and more (similar names, the same address).</span>
      </div>
    </div>
  );
}

function SideReportCard({ r, title }: { r: SideReport; title: string }) {
  const tone = { error: "text-flare", warning: "text-brass", info: "text-tide" } as const;
  const Icon = { error: X, warning: AlertTriangle, info: Info } as const;
  return (
    <div className={`rounded-xl border px-4 py-3 ${r.can_load ? "border-rule" : "border-flare/40 bg-flare/[0.03]"}`}>
      <div className="flex items-center gap-2">
        <span className="text-[13px] font-semibold text-fog">{title}</span>
        <span className="truncate text-[12px] text-mist">{r.filename}</span>
        <span className={`ml-auto shrink-0 text-[12px] font-semibold ${r.can_load ? "text-kelp" : "text-flare"}`}>
          {r.can_load ? `${fmt(r.loaded)} of ${fmt(r.rows)} rows ready` : `${r.errors} problem${r.errors === 1 ? "" : "s"} to fix`}
        </span>
      </div>
      {r.issues.length === 0 && <div className="mt-2 flex items-center gap-1.5 text-[12.5px] text-kelp"><Check className="h-4 w-4" /> No problems found.</div>}
      <ul className="mt-2 space-y-2">
        {r.issues.map((i) => {
          const I = Icon[i.level];
          return (
            <li key={i.code} className="text-[12.5px] leading-snug">
              <div className={`flex items-start gap-1.5 ${tone[i.level]}`}>
                <I className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span className="text-fog"><b className={tone[i.level]}>{fmt(i.count)}</b> — {i.message}</span>
              </div>
              {i.examples.length > 0 && (
                <div className="ml-5 mt-0.5 text-[11.5px] text-mist">
                  e.g. {i.examples.slice(0, 3).map((x) => [x.row != null ? `row ${x.row}` : null, x.value ? `"${x.value}"` : null].filter(Boolean).join(" ")).join(" · ")}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
