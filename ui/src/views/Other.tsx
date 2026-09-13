import { Database, GitBranch, ShieldCheck } from "lucide-react";
import { Diff } from "../panels/Diff";
import { Gate } from "../panels/Gate";
import { Narrative } from "../panels/Narrative";
import { Score } from "../panels/Score";
import { Strip } from "../panels/Strip";
import type { Branch, State } from "../types";
import { fmt, short, statusTone } from "../ui";

function Title({ icon: Icon, title, sub }: { icon: React.ElementType; title: string; sub: string }) {
  return (
    <div className="flex items-start gap-3">
      <Icon className="mt-1 h-6 w-6 text-navy" />
      <div>
        <h1 className="text-2xl font-bold text-fog">{title}</h1>
        <p className="text-[13px] text-mist">{sub}</p>
      </div>
    </div>
  );
}

export function DiffView({ s, branch, canAct, replay, onFocus }: {
  s: State; branch: Branch | null; canAct: boolean; replay: boolean; onFocus: (id: string) => void;
}) {
  return (
    <div className="space-y-4">
      <Title icon={GitBranch} title="Diff Viewer" sub="The exact row-level change a branch would make to GOLDEN — observed in the database, not estimated." />
      <div className="card overflow-hidden"><Strip s={s} focus={branch?.id ?? null} onFocus={onFocus} canAct={canAct} /></div>
      <div className="h-[calc(100vh-330px)] min-h-[480px]"><Diff branch={branch} mapping={s.mapping} canAct={canAct} replay={replay} /></div>
    </div>
  );
}

export function GateView({ s, branch, canAct, onFocus }: { s: State; branch: Branch | null; canAct: boolean; onFocus: (id: string) => void }) {
  const gated = s.order.map((id) => s.branches[id]).filter((b) => b.gate);
  return (
    <div className="space-y-4">
      <Title icon={ShieldCheck} title="Merge Gate" sub="Every merge request is weighed against the tier's limits. Held requests wait here for a person." />
      <div className="grid grid-cols-[380px_1fr] gap-4">
        <section className="card overflow-hidden">
          <div className="border-b border-rule px-4 py-3 text-[13px] font-semibold text-fog">Merge requests ({gated.length})</div>
          <ul>
            {gated.length === 0 && <li className="px-4 py-6 text-[13px] text-mist">No merge requests yet.</li>}
            {gated.map((b) => (
              <li key={b.id}>
                <button onClick={() => onFocus(b.id)} className={`flex w-full items-center gap-3 border-b border-rule px-4 py-3 text-left ${branch?.id === b.id ? "bg-tide/5" : "hover:bg-deck/50"}`}>
                  <div className="min-w-0 flex-1">
                    <div className="text-[13px] font-semibold text-fog">{b.cls || b.id}</div>
                    <div className="truncate text-[12px] text-mist">{b.gate?.reason}</div>
                  </div>
                  <span className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                    { kelp: "bg-kelp/10 text-kelp", brass: "bg-brass/10 text-brass", flare: "bg-flare/10 text-flare", sky: "bg-sky/10 text-sky", tide: "bg-tide/10 text-tide", mist: "bg-deck text-mist" }[statusTone(b.status)]}`}>{b.status}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
        <div className="space-y-4">
          <Gate branch={branch} canAct={canAct} />
          {branch?.request && (
            <section className="card px-5 py-4 text-[13px]">
              <div className="font-semibold text-fog">Agent's request</div>
              <p className="mt-1 text-mist">{branch.request.rationale}</p>
              {branch.selfcheck && <p className="mt-2 text-fog">Self-check: {branch.selfcheck.note} (confidence {branch.selfcheck.confidence_before} → {branch.selfcheck.confidence_after})</p>}
            </section>
          )}
        </div>
      </div>
    </div>
  );
}

export function RunsView({ s, onFocus }: { s: State; onFocus: (id: string) => void }) {
  const runs = Object.values(s.runs);
  return (
    <div className="space-y-4">
      <Title icon={GitBranch} title="Runs" sub="Every run, its A/B scores, and the agent's full narrative." />
      <div className="grid grid-cols-[1fr_1fr] gap-4">
        <div className="space-y-4">
          <section className="card overflow-hidden">
            <table className="w-full text-[13px]">
              <thead className="bg-deck/70 text-left text-[12px] text-fog/80"><tr>
                <th className="px-4 py-2 font-medium">Run</th><th className="font-medium">Mode</th><th className="font-medium">Tier</th>
                <th className="font-medium">Gate</th><th className="font-medium">Started</th><th className="pr-4 font-medium">Status</th></tr></thead>
              <tbody>
                {runs.length === 0 && <tr><td colSpan={6} className="px-4 py-6 text-mist">No runs yet.</td></tr>}
                {runs.map((r) => (
                  <tr key={r.id} className="border-t border-rule">
                    <td className="px-4 py-2.5 font-mono text-[12px] text-fog">{r.id}</td><td>{r.mode}</td><td>{r.tier}</td>
                    <td>{r.gateEnabled ? "on" : "off (control)"}</td>
                    <td>{new Date(r.startedAt).toLocaleString("en-GB", { hour: "2-digit", minute: "2-digit", day: "2-digit", month: "short" })}</td>
                    <td className="pr-4">{r.ended ? r.ended.status : "running"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
          <Score s={s} />
        </div>
        <div className="h-[calc(100vh-240px)] min-h-[480px]"><Narrative s={s} onFocus={onFocus} /></div>
      </div>
    </div>
  );
}

export function DatabaseView({ s }: { s: State }) {
  const mats = s.order.map((id) => s.branches[id]).filter((b) => b.materialised);
  return (
    <div className="space-y-4">
      <Title icon={Database} title="Database" sub="What the event stream reports about Exasol: GOLDEN's fingerprint history and every branch copy." />
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
