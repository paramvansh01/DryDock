import { AlertTriangle, BarChart3, Check, ChevronDown, CircleHelp, Clock, Database, FileText, Home, Layers, Link as LinkIcon, Link2, ShieldCheck, Upload, Users, Workflow, X } from "lucide-react";
import { useState } from "react";
import { reviewer, setReviewer } from "../api";
import type { Kpis, Step } from "../derive";
import { pct } from "../derive";
import { toast } from "../ui";

export type Tab = "data" | "overview" | "reconcile" | "diff" | "gate" | "runs" | "database" | "system";
export const TAB_IDS: Tab[] = ["data", "overview", "reconcile", "diff", "gate", "runs", "database", "system"];

const TABS: { id: Tab; label: string; icon: React.ElementType }[] = [
  { id: "data", label: "Your Data", icon: Upload },
  { id: "overview", label: "Overview", icon: Home },
  { id: "reconcile", label: "Reconcile", icon: Link2 },
  { id: "diff", label: "Diff Viewer", icon: FileText },
  { id: "gate", label: "Merge Gate", icon: ShieldCheck },
  { id: "runs", label: "Runs", icon: BarChart3 },
  { id: "database", label: "Database", icon: Database },
  { id: "system", label: "Live System", icon: Workflow },
];

export function Nav({ tab, onTab, status, pending, onHelp }: {
  tab: Tab; onTab: (t: Tab) => void; status: string; pending: number; onHelp: () => void;
}) {
  const live = status === "live";
  const replay = status.startsWith("replay");
  return (
    <nav className="sticky top-0 z-30 border-b border-rule bg-hull/95 backdrop-blur">
      <div className="mx-auto flex h-16 max-w-[1440px] items-center gap-4 px-6">
        <div className="flex shrink-0 items-center gap-2.5">
          <Layers className="h-8 w-8 text-navy" strokeWidth={2.2} />
          <div className="leading-tight">
            <div className="text-lg font-extrabold tracking-tight text-fog">DRYDOCK</div>
            <div className="text-[11px] text-mist">Safe writes for AI agents</div>
          </div>
        </div>
        <div className="flex h-full flex-1 items-stretch justify-center gap-0.5">
          {TABS.map(({ id, label, icon: Icon }) => (
            <button key={id} onClick={() => onTab(id)} data-tour={`tab-${id}`}
              className={`relative flex flex-col items-center justify-center gap-1 px-3.5 text-[12px] font-medium transition-colors
                ${tab === id ? "bg-tide/5 text-tide" : "text-fog/80 hover:text-tide"}`}>
              <Icon className="h-[18px] w-[18px]" />
              <span className="whitespace-nowrap">{label}</span>
              {id === "gate" && pending > 0 && (
                <span className="absolute right-1.5 top-2.5 grid h-4 min-w-4 place-items-center rounded-full bg-brass px-1 text-[10px] font-bold text-white">{pending}</span>
              )}
              {tab === id && <span className="absolute inset-x-3 bottom-0 h-0.5 rounded bg-tide" />}
            </button>
          ))}
        </div>
        <div className="flex shrink-0 items-center gap-2 text-[12px]" data-tour="status"
          title="Whether this page is receiving Drydock's live updates (the page itself never talks to the database)">
          <span className={`h-2 w-2 rounded-full ${live ? "bg-kelp" : replay ? "bg-brass" : "bg-flare"}`} />
          <span className="whitespace-nowrap text-fog/80">{live ? "Connected (live)" : replay ? "Replay — not live" : status === "offline" ? "Server offline" : "Connecting…"}</span>
        </div>
        <button onClick={onHelp} data-tour="help" title="Show me around: a guided tour of every section"
          className="grid h-9 w-9 shrink-0 place-items-center rounded-full border border-rule text-mist hover:border-tide hover:text-tide">
          <CircleHelp className="h-[18px] w-[18px]" />
        </button>
        <ReviewerBadge />
      </div>
    </nav>
  );
}

function initials(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length || name === "human") return "HR";
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : "")).toUpperCase();
}

/** Who is reviewing. Every approval, rejection and undo is recorded under this name and appears in the audit log. */
function ReviewerBadge() {
  const [name, setName] = useState(reviewer());
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(name === "human" ? "" : name);
  const save = () => {
    const v = draft.trim();
    if (v && !/^[\w .,'@()-]{1,64}$/u.test(v)) { toast("warn", "Use letters, digits, spaces and . , ' @ ( ) - only"); return; }
    setReviewer(v || "human");
    setName(v || "human");
    setOpen(false);
    toast("good", v ? `Decisions will be recorded as ${v}` : "Decisions will be recorded as 'human'");
  };
  return (
    <div className="relative shrink-0 border-l border-rule pl-4" data-tour="reviewer">
      <button onClick={() => setOpen((o) => !o)} className="flex items-center gap-2" title="Set the name your decisions are recorded under">
        <span className="grid h-9 w-9 place-items-center rounded-full bg-navy text-[12px] font-semibold text-white">{initials(name)}</span>
        <span className="max-w-[120px] truncate text-[13px] font-medium text-fog">{name === "human" ? "Human reviewer" : name}</span>
        <ChevronDown className="h-4 w-4 text-mist" />
      </button>
      {open && (
        <div className="card absolute right-0 top-12 z-40 w-[300px] px-4 py-3.5 shadow-lg">
          <div className="text-[13px] font-semibold text-fog">Your name</div>
          <p className="mt-0.5 text-[12px] text-mist">Every approval, rejection and undo is recorded under this name, in the audit log too.</p>
          <input autoFocus value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={(e) => e.key === "Enter" && save()}
            placeholder="e.g. Priya Shah" className="mt-2 w-full rounded-lg border border-rule px-3 py-2 text-[13px] outline-none focus:border-tide" />
          <div className="mt-2.5 flex justify-end gap-2">
            <button onClick={() => setOpen(false)} className="rounded-lg border border-rule px-3 py-1.5 text-[12.5px]">Cancel</button>
            <button onClick={save} className="rounded-lg bg-navy px-3 py-1.5 text-[12.5px] font-semibold text-white">Save</button>
          </div>
        </div>
      )}
    </div>
  );
}

export function Stepper({ steps }: { steps: Step[] }) {
  return (
    <div className="card flex items-center gap-2 px-5 py-4">
      {steps.map((s, i) => (
        <div key={s.n} className="flex flex-1 items-center gap-3">
          <span className={`grid h-9 w-9 shrink-0 place-items-center rounded-full text-sm font-semibold
            ${s.status === "done" ? "bg-kelp text-white" : s.status === "current" ? "bg-tide text-white"
              : s.status === "failed" ? "bg-flare text-white" : "bg-deck text-mist"}`}>
            {s.status === "done" ? <Check className="h-5 w-5" /> : s.status === "failed" ? <X className="h-5 w-5" /> : s.n}
          </span>
          <div className="min-w-0 leading-tight">
            <div className="text-[13px] font-medium text-fog">{s.status === "done" ? <span className="mr-1 text-mist">{s.n}</span> : null}{s.label}</div>
            <div className={`text-[11px] ${s.status === "current" ? "text-tide" : s.status === "failed" ? "text-flare" : "text-mist"}`}>{s.note}</div>
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
      <KpiCard icon={Users} tone="bg-tide/10 text-tide" label="Candidate Pairs" value={k.candidates} sub="System A × System B" />
      <KpiCard icon={LinkIcon} tone="bg-kelp/10 text-kelp" label="Auto-matched" value={k.autoMatched}
        sub={auto == null ? "all 3 matchers agree" : <span className="text-kelp">↑ {auto}% (3 of 3 agree)</span>} />
      <KpiCard icon={Clock} tone="bg-brass/10 text-brass" label="Needs Review" value={k.needsReview} sub="matchers split" />
      <KpiCard icon={AlertTriangle} tone="bg-flare/10 text-flare" label="Rejected" value={k.rejected}
        sub={rej == null ? "all 3 said no" : <span className="text-flare">↓ {rej}% (3 of 3 said no)</span>} />
      {withGolden && <KpiCard icon={Database} tone="bg-tide/10 text-tide" label="GOLDEN Dataset" value={k.golden} sub="Current records" />}
    </div>
  );
}

