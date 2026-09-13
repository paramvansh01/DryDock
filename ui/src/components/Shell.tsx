import { AlertTriangle, BarChart3, Check, ChevronDown, Clock, Database, FileText, Home, Layers, Link as LinkIcon, Link2, ShieldCheck, Users, Workflow } from "lucide-react";
import type { Kpis, Step } from "../derive";
import { pct } from "../derive";

export type Tab = "overview" | "reconcile" | "diff" | "gate" | "runs" | "database" | "system";

const TABS: { id: Tab; label: string; icon: React.ElementType }[] = [
  { id: "overview", label: "Overview", icon: Home },
  { id: "reconcile", label: "Reconcile", icon: Link2 },
  { id: "diff", label: "Diff Viewer", icon: FileText },
  { id: "gate", label: "Merge Gate", icon: ShieldCheck },
  { id: "runs", label: "Runs", icon: BarChart3 },
  { id: "database", label: "Database", icon: Database },
  { id: "system", label: "Live System", icon: Workflow },
];

export function Nav({ tab, onTab, status, pending }: { tab: Tab; onTab: (t: Tab) => void; status: string; pending: number }) {
  const live = status === "live";
  const replay = status.startsWith("replay");
  return (
    <nav className="sticky top-0 z-30 border-b border-rule bg-hull/95 backdrop-blur">
      <div className="mx-auto flex h-16 max-w-[1400px] items-center gap-6 px-6">
        <div className="flex items-center gap-2.5">
          <Layers className="h-8 w-8 text-navy" strokeWidth={2.2} />
          <div className="leading-tight">
            <div className="text-lg font-extrabold tracking-tight text-fog">DRYDOCK</div>
            <div className="text-[11px] text-mist">Safe writes for AI agents</div>
          </div>
        </div>
        <div className="flex h-full flex-1 items-stretch justify-center gap-1">
          {TABS.map(({ id, label, icon: Icon }) => (
            <button key={id} onClick={() => onTab(id)}
              className={`relative flex flex-col items-center justify-center gap-1 px-5 text-[12px] font-medium transition-colors
                ${tab === id ? "bg-tide/5 text-tide" : "text-fog/80 hover:text-tide"}`}>
              <Icon className="h-[18px] w-[18px]" />
              {label}
              {id === "gate" && pending > 0 && (
                <span className="absolute right-3 top-2.5 grid h-4 min-w-4 place-items-center rounded-full bg-brass px-1 text-[10px] font-bold text-white">{pending}</span>
              )}
              {tab === id && <span className="absolute inset-x-3 bottom-0 h-0.5 rounded bg-tide" />}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2 text-[12px]" title="Status of the orchestrator's event stream (the UI never talks to Exasol directly)">
          <span className={`h-2 w-2 rounded-full ${live ? "bg-kelp" : replay ? "bg-brass" : "bg-flare"}`} />
          <span className="text-fog/80">{live ? "Connected (live)" : replay ? "Replay — not live" : status === "offline" ? "Orchestrator offline" : "Connecting…"}</span>
        </div>
        <div className="flex items-center gap-2 border-l border-rule pl-5" title="Approvals and rejections are recorded as the reviewer">
          <span className="grid h-9 w-9 place-items-center rounded-full bg-navy text-[12px] font-semibold text-white">HR</span>
          <span className="text-[13px] font-medium text-fog">Human reviewer</span>
          <ChevronDown className="h-4 w-4 text-mist" />
        </div>
      </div>
    </nav>
  );
}

export function Stepper({ steps }: { steps: Step[] }) {
  return (
    <div className="card flex items-center gap-2 px-5 py-4">
      {steps.map((s, i) => (
        <div key={s.n} className="flex flex-1 items-center gap-3">
          <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-full text-sm font-semibold
            ${s.status === "done" ? "bg-kelp text-white" : s.status === "current" ? "bg-tide text-white" : "bg-deck text-mist"}`}>
            {s.status === "done" ? <Check className="h-5 w-5" /> : s.n}
          </span>
          <div className="min-w-0 leading-tight">
            <div className="text-[13px] font-medium text-fog">{s.status === "done" ? <span className="mr-1 text-mist">{s.n}</span> : null}{s.label}</div>
            <div className={`text-[11px] ${s.status === "current" ? "text-tide" : "text-mist"}`}>{s.note}</div>
          </div>
          {i < steps.length - 1 && <div className="mx-2 h-px flex-1 bg-rule" />}
        </div>
      ))}
    </div>
  );
}

const fmt = (n: number | null) => (n == null ? "—" : n.toLocaleString("en-GB"));

function KpiCard({ icon: Icon, tone, label, value, sub, first }: {
  icon: React.ElementType; tone: string; label: string; value: number | null; sub: React.ReactNode; first?: boolean;
}) {
  return (
    <div className="card flex items-center gap-4 px-4 py-4">
      <span className={`grid h-12 w-12 shrink-0 place-items-center rounded-xl ${tone}`}><Icon className="h-6 w-6" /></span>
      <div className="min-w-0">
        {!first && <div className="text-[12px] text-mist">{label}</div>}
        <div className="num text-2xl font-bold text-fog">{fmt(value)}</div>
        {first && <div className="text-[13px] text-fog">{label}</div>}
        <div className="truncate text-[12px] text-mist">{sub}</div>
      </div>
    </div>
  );
}

export function KpiRow({ k, withGolden = true }: { k: Kpis; withGolden?: boolean }) {
  const auto = pct(k.autoMatched, k.scored), rej = pct(k.rejected, k.scored);
  return (
    <div className={`grid gap-4 ${withGolden ? "grid-cols-5" : "grid-cols-4"}`}>
      <KpiCard icon={Users} tone="bg-tide/10 text-tide" label="Candidate Pairs" value={k.candidates} sub="SOURCE_A × SOURCE_B" />
      <KpiCard icon={LinkIcon} tone="bg-kelp/10 text-kelp" label="Auto-matched" value={k.autoMatched}
        sub={auto == null ? "all 3 matchers agree" : <span className="text-kelp">↑ {auto}% (3 of 3 agree)</span>} />
      <KpiCard icon={Clock} tone="bg-brass/10 text-brass" label="Needs Review" value={k.needsReview} sub="matchers split" />
      <KpiCard icon={AlertTriangle} tone="bg-flare/10 text-flare" label="Rejected" value={k.rejected}
        sub={rej == null ? "all 3 said no" : <span className="text-flare">↓ {rej}% (3 of 3 said no)</span>} />
      {withGolden && <KpiCard icon={Database} tone="bg-tide/10 text-tide" label="GOLDEN Dataset" value={k.golden} sub="Current records" />}
    </div>
  );
}

