import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, Check, FileText, Play, Sparkles, Upload, X } from "lucide-react";
import { Suspense, lazy, useEffect, useMemo, useReducer, useState } from "react";
import { AUTH_EVENT, api, setAccessToken } from "./api";
import { KpiRow, Nav, Stepper, TAB_IDS, type Tab } from "./components/Shell";
import { explainError, kpis, steps } from "./derive";
import { initialState, reduce } from "./reducer";
import { type Source, connect } from "./stream";
import type { DatasetInfo, DrydockEvent } from "./types";
import { Toaster, toast } from "./ui";
import { DiffView, GateView, RunsView } from "./views/Other";
import { Overview } from "./views/Overview";
import { Reconcile } from "./views/Reconcile";
import { Tour, TourOffer } from "./views/Tour";

// The heavier tabs load when first opened, so the first screen arrives sooner.
const DataView = lazy(() => import("./views/Data").then((m) => ({ default: m.DataView })));
const DatabaseView = lazy(() => import("./views/Database").then((m) => ({ default: m.DatabaseView })));
const SystemView = lazy(() => import("./views/System").then((m) => ({ default: m.SystemView })));

// Source defaults to LIVE (the orchestrator's WebSocket). The illustrative fixture is a
// development aid only: it is excluded from production builds (vite.config.ts) and, when
// played, the whole screen carries an unmissable REPLAY / ILLUSTRATIVE watermark.
const DEV = import.meta.env.DEV;

function initialSource(): Source {
  const q = new URLSearchParams(location.search);
  const r = q.get("replay");
  if (r === "fixture" && DEV) return { kind: "fixture" };
  if (r && r !== "fixture") return { kind: "server", path: r };
  return { kind: "live" };
}

export default function App() {
  const [source, setSource] = useState<Source>(initialSource);
  const [state, dispatch] = useReducer((s: typeof initialState, e: DrydockEvent) => reduce(s, e), initialState);
  const [status, setStatus] = useState("connecting");
  const [tab, setTab] = useState<Tab>(() => {
    const t = new URLSearchParams(location.search).get("tab");
    return (TAB_IDS.includes(t as Tab) ? t : "reconcile") as Tab;
  });
  const [tour, setTour] = useState(false);
  const [locked, setLocked] = useState(false);
  useEffect(() => {
    const ask = () => setLocked(true);
    window.addEventListener(AUTH_EVENT, ask);
    return () => window.removeEventListener(AUTH_EVENT, ask);
  }, []);
  const [offerTour, setOfferTour] = useState(false);
  const [pinned, setPinned] = useState<string | null>(null);
  const [newRun, setNewRun] = useState(false);
  const [flash, setFlash] = useState<{ id: string; match: boolean } | null>(null);

  useEffect(() => connect(source, dispatch, setStatus), [source]);

  const focusId = pinned && state.branches[pinned] ? pinned : state.focus;
  const branch = focusId ? state.branches[focusId] : null;
  const replay = source.kind !== "live" || !!state.replay;
  const canAct = !replay && status === "live";
  const pending = Object.values(state.branches).filter((b) => b.status === "PENDING").length;
  const ds = state.system.snapshot?.config.dataset;
  const run = state.activeRun ? state.runs[state.activeRun] : null;
  const runError = run?.ended && run.ended.status !== "OK" ? explainError(run.ended.error) : null;

  // The visual undo: when an unmerge lands for the branch on screen, show the split and the fingerprint badge.
  const lastUnmerge = useMemo(() => Object.values(state.branches).find((b) => b.unmerged && b.id === focusId), [state.branches, focusId]);
  useEffect(() => {
    if (!lastUnmerge?.unmerged) return;
    setFlash({ id: lastUnmerge.id, match: lastUnmerge.unmerged.fingerprint_match });
    const t = setTimeout(() => setFlash(null), 2400);
    return () => clearTimeout(t);
  }, [lastUnmerge?.unmerged?.restored_fingerprint]); // eslint-disable-line react-hooks/exhaustive-deps

  const focus = (id: string) => setPinned(id);
  const openDiff = (id: string) => { setPinned(id); setTab("diff"); };
  const playExample = () => { setSource({ kind: "fixture" }); setTab("reconcile"); };

  return (
    <div className="min-h-full">
      <Nav tab={tab} onTab={setTab} status={status} pending={pending} onHelp={() => setTour(true)} />
      {replay && (
        <div className="pointer-events-none fixed inset-0 z-50 grid place-items-center">
          <div className="-rotate-12 select-none text-center text-5xl font-black uppercase tracking-widest text-brass/10">
            replay{state.illustrative ? " · illustrative" : ""}<br />
            <span className="text-2xl">{state.illustrative ? "not a real run" : "recorded run, not live"}</span>
          </div>
        </div>
      )}
      {replay && (
        <div className="border-b border-brass/30 bg-brass/10 px-6 py-1.5 text-center text-xs text-brass">
          REPLAY{state.illustrative ? " · ILLUSTRATIVE FIXTURE — every number is invented; never present this as a result" : ` of ${source.kind === "server" ? source.path : "a recording"} — not live`}
          <button className="ml-3 font-medium underline" onClick={() => { setSource({ kind: "live" }); location.search = ""; }}>go live</button>
        </div>
      )}
      {status === "offline" && !replay && (
        <div className="border-b border-flare/30 bg-flare/10 px-6 py-1.5 text-center text-xs text-flare">
          Orchestrator not reachable (uv run uvicorn drydock.orchestrator:app --port 8765). Retrying…
        </div>
      )}

      <main className="mx-auto max-w-[1400px] space-y-4 px-6 py-6">
        <Suspense fallback={<div className="px-2 py-10 text-center text-[13px] text-mist">Loading…</div>}>
        {tab === "reconcile" && (
          <>
            <div className="flex items-end justify-between">
              <div>
                <div className="flex items-center gap-2 text-[11px] font-medium uppercase tracking-[0.16em] text-mist"><FileText className="h-3.5 w-3.5" /> Entity resolution</div>
                <h1 className="mt-1 text-3xl font-extrabold tracking-tight text-fog">Customer Database Reconciliation</h1>
                <p className="mt-1 text-[14px] text-fog/80">Match and merge the customers of System A and System B into one clean list (GOLDEN).
                  {ds && <> Working on <b>{ds.label}</b>.</>}</p>
              </div>
              <div className="flex items-center gap-3">
                {DEV && (
                  <button onClick={playExample} title="Plays the illustrative fixture (invented numbers, dev only)"
                    className="flex items-center gap-2 rounded-lg border border-rule bg-hull px-5 py-2.5 text-[14px] font-medium text-fog hover:border-tide">
                    <FileText className="h-4 w-4" /> Load Example
                  </button>
                )}
                <button onClick={() => setNewRun(true)} disabled={replay || status !== "live"} data-tour="start-run"
                  title={replay || status !== "live" ? "Needs the live orchestrator" : "Start a reconciliation run"}
                  className="flex items-center gap-2 rounded-lg bg-navy px-6 py-2.5 text-[14px] font-semibold text-white shadow-sm disabled:opacity-40">
                  <Play className="h-4 w-4 fill-white" /> Start New Run
                </button>
              </div>
            </div>
            {runError && (
              <div className="card flex items-start gap-3 border-flare/40 bg-flare/[0.04] px-5 py-3.5">
                <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-flare" />
                <div className="min-w-0 text-[13px]">
                  <div className="font-semibold text-fog">The last run stopped: {runError.title.charAt(0).toLowerCase() + runError.title.slice(1)}</div>
                  <div className="mt-0.5 text-fog/80">{runError.body}</div>
                  {runError.fix && <div className="mt-1.5 rounded-md bg-hull px-2.5 py-1.5 text-fog"><b>How to fix: </b>{runError.fix}</div>}
                </div>
              </div>
            )}
            {Object.keys(state.runs).length === 0 && !replay && (
              <div className="card flex items-center gap-4 border-tide/30 bg-tide/[0.04] px-5 py-4">
                <Sparkles className="h-7 w-7 shrink-0 text-tide" />
                <div className="min-w-0 text-[13px]">
                  <div className="text-[14.5px] font-semibold text-fog">New here? Two steps to your first result</div>
                  <div className="text-fog/80">1. Choose the data: your own two files, or the demo that is already loaded. &nbsp; 2. Press <b>Start New Run</b>.
                    Drydock finds the duplicates, and nothing changes until you approve it.</div>
                </div>
                <div className="ml-auto flex shrink-0 gap-2">
                  <button onClick={() => setTour(true)} className="flex items-center gap-1.5 rounded-lg border border-rule bg-hull px-3.5 py-2 text-[13px]"><Sparkles className="h-4 w-4 text-tide" /> Take the tour</button>
                  <button onClick={() => setTab("data")} className="flex items-center gap-1.5 rounded-lg border border-rule bg-hull px-3.5 py-2 text-[13px]"><Upload className="h-4 w-4 text-tide" /> Use my own data</button>
                </div>
              </div>
            )}
            <div data-tour="stepper"><Stepper steps={steps(state)} /></div>
            <div data-tour="kpis"><KpiRow k={kpis(state)} /></div>
            <Reconcile s={state} canAct={canAct} onOpenDiff={openDiff} />
          </>
        )}
        {tab === "data" && <DataView s={state} canAct={canAct} onGoRun={() => { setTab("reconcile"); setNewRun(true); }}
                                     onTour={() => setTour(true)} onLoaded={() => setOfferTour(true)} />}
        {tab === "overview" && <Overview s={state} onReview={() => setTab("reconcile")} onExample={playExample} canExample={DEV} />}
        {tab === "diff" && <DiffView s={state} branch={branch} canAct={canAct} replay={replay} onFocus={focus} />}
        {tab === "gate" && <GateView s={state} branch={branch?.gate ? branch : Object.values(state.branches).find((b) => b.status === "PENDING") ?? branch} canAct={canAct} onFocus={focus} />}
        {tab === "runs" && <RunsView s={state} onFocus={openDiff} />}
        {tab === "database" && <DatabaseView s={state} replay={replay} />}
        {tab === "system" && <SystemView s={state} canAct={canAct} />}
        </Suspense>
      </main>

      <footer className="mx-auto flex max-w-[1400px] items-center justify-between border-t border-rule px-6 py-4 text-[12px] text-mist">
        <span><span className="font-semibold text-fog">DRYDOCK</span> v0.1.0</span>
        <span>Governed changes. Observed impact.</span>
      </footer>

      {newRun && <NewRunDialog onClose={() => setNewRun(false)} lastRun={state.activeRun} ds={ds} />}
      {offerTour && <TourOffer onNo={() => setOfferTour(false)} onYes={() => { setOfferTour(false); setTour(true); }} />}
      <Tour open={tour} onClose={() => setTour(false)} setTab={setTab} />
      <Toaster />
      {locked && <TokenPrompt />}

      <AnimatePresence>
        {flash && (
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
            className="pointer-events-none fixed inset-0 z-40 grid place-items-center bg-fog/30">
            <div className="flex items-center gap-6">
              <motion.div initial={{ x: 60 }} animate={{ x: -40 }} transition={{ duration: 0.9 }} className="card px-6 py-4 font-mono text-tide">GOLDEN (pre-merge)</motion.div>
              <motion.div initial={{ x: -60 }} animate={{ x: 40 }} transition={{ duration: 0.9 }} className="card px-6 py-4 font-mono text-mist">merged image → __UNDONE_</motion.div>
            </div>
            <div className={`absolute mt-40 flex items-center gap-2 text-2xl font-bold ${flash.match ? "text-kelp" : "text-flare"}`}>
              {flash.match ? <><Check className="h-7 w-7" strokeWidth={3} />Fingerprint match</> : <><X className="h-7 w-7" strokeWidth={3} />Fingerprint mismatch</>}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function NewRunDialog({ onClose, lastRun, ds }: { onClose: () => void; lastRun: string | null; ds?: DatasetInfo }) {
  const stamp = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
  const [runId, setRunId] = useState(`run_${stamp}`);
  const [mode, setMode] = useState("scripted");
  const [gate, setGate] = useState(true);
  const [tier, setTier] = useState(2);
  const [planFrom, setPlanFrom] = useState(lastRun ?? "");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const start = async () => {
    setBusy(true);
    setErr(null);
    try {
      await api.startRun({ run_id: runId, mode, gate_enabled: gate, tier, plan_from_run: mode === "treatment" ? planFrom : null });
      toast("info", "Run started", "Watch the steps light up. Riskier changes will wait for you in the Merge Gate.");
      onClose();
    } catch (e) { const x = explainError(e instanceof Error ? e.message : String(e)); setErr(x.fix ? `${x.title}. ${x.fix}` : x.body || x.title); }
    setBusy(false);
  };
  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-fog/30">
      <div className="card w-[460px] px-6 py-5">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-bold text-fog">Start a new run</h2>
          <button onClick={onClose} className="text-mist"><X className="h-5 w-5" /></button>
        </div>
        {ds && <p className="mt-1 text-[12.5px] text-mist">Working on <b className="text-fog">{ds.label}</b> · {ds.rows_a.toLocaleString("en-GB")} + {ds.rows_b.toLocaleString("en-GB")} records</p>}
        <div className="mt-4 space-y-3 text-[13px]">
          <label className="block">Run id<input value={runId} onChange={(e) => setRunId(e.target.value)} className="mt-1 w-full rounded-lg border border-rule px-3 py-2" /></label>
          <label className="block">Mode
            <select value={mode} onChange={(e) => setMode(e.target.value)} className="mt-1 w-full rounded-lg border border-rule px-3 py-2">
              <option value="scripted">scripted — fixed SQL, no AI planning</option>
              <option value="agent" disabled={ds ? !ds.agent : false}>agent — Gemini plans, Drydock gates{ds && !ds.agent ? " (demo data only)" : ""}</option>
              <option value="treatment">A/B treatment — replay a run's plan with the gate on</option>
            </select>
          </label>
          {mode === "treatment" && <label className="block">Replay plan of run<input value={planFrom} onChange={(e) => setPlanFrom(e.target.value)} className="mt-1 w-full rounded-lg border border-rule px-3 py-2" /></label>}
          <div className="flex gap-4">
            <label className="flex items-center gap-2"><input type="checkbox" checked={gate} onChange={(e) => setGate(e.target.checked)} /> Gate enabled {gate ? "" : "(A/B control)"}</label>
            <label className="flex items-center gap-2">Tier
              <select value={tier} onChange={(e) => setTier(Number(e.target.value))} className="rounded-md border border-rule px-2 py-1">{[0, 1, 2, 3].map((t) => <option key={t}>{t}</option>)}</select>
            </label>
          </div>
          <p className="text-[12px] leading-snug text-mist">
            {mode === "scripted" ? "A fixed, repeatable plan. The best choice for your own data."
              : mode === "agent" ? "Gemini plans the steps; Drydock still gates every change."
              : "Replays an earlier run's exact plan, to compare with and without the gate."}
            {" "}Tier sets how much merges on its own: 2 is a sensible default, 0 means everything waits for you.
            {!gate && <span className="text-brass"> With the gate off, every change merges without review: for comparisons only.</span>}
          </p>
          {err && <p className="text-flare">{err}</p>}
        </div>
        <div className="mt-5 flex justify-end gap-2">
          <button onClick={onClose} className="rounded-lg border border-rule px-4 py-2 text-[13px]">Cancel</button>
          <button disabled={busy || !runId} onClick={start} className="rounded-lg bg-navy px-5 py-2 text-[13px] font-semibold text-white disabled:opacity-40">Start</button>
        </div>
      </div>
    </div>
  );
}

/** A protected Drydock (DRYDOCK_ACCESS_TOKEN on the server) asks once; the token is kept in a same-site cookie. */
function TokenPrompt() {
  const [t, setT] = useState("");
  return (
    <div className="fixed inset-0 z-[80] grid place-items-center bg-fog/40">
      <form className="card w-[420px] px-6 py-5" onSubmit={(e) => { e.preventDefault(); if (t.trim()) { setAccessToken(t); location.reload(); } }}>
        <h2 className="text-lg font-bold text-fog">This Drydock is protected</h2>
        <p className="mt-1 text-[13px] text-fog/80">Enter the access token you were given. It is kept in this browser, so you only need to do this once.</p>
        <input autoFocus type="password" value={t} onChange={(e) => setT(e.target.value)} placeholder="Access token"
          className="mt-3 w-full rounded-lg border border-rule px-3 py-2 text-[13px] outline-none focus:border-tide" />
        <div className="mt-3 flex justify-end"><button className="rounded-lg bg-navy px-5 py-2 text-[13px] font-semibold text-white">Continue</button></div>
      </form>
    </div>
  );
}
