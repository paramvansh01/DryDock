import { ArrowRight, Clock, Eye, ListChecks, Play, UserRound } from "lucide-react";
import { activity, elapsedMinutes, kpis, runProgress, steps } from "../derive";
import type { State } from "../types";
import { KpiRow, Stepper } from "../components/Shell";

const DOT: Record<string, string> = { good: "bg-kelp", bad: "bg-flare", warn: "bg-brass", disclose: "bg-sky", info: "bg-tide" };

export function Overview({ s, onReview, onExample, canExample }: {
  s: State; onReview: () => void; onExample: () => void; canExample: boolean;
}) {
  const k = kpis(s);
  const run = s.activeRun ? s.runs[s.activeRun] : null;
  const prog = runProgress(s);
  const mins = elapsedMinutes(s);
  const review = k.needsReview;
  return (
    <div className="space-y-5">
      <div className="flex items-end justify-between">
        <div>
          <div className="text-[11px] font-medium uppercase tracking-[0.18em] text-mist">Customer reconciliation</div>
          <h1 className="mt-2 text-4xl font-extrabold tracking-tight text-fog">Unify your customer data</h1>
          <p className="mt-2 text-[15px] text-fog/80">
            An AI agent finds, compares and proposes merges of duplicate customers — you see the exact
            row-level diff and decide before anything reaches GOLDEN.
          </p>
        </div>
        <p className="max-w-[260px] shrink-0 text-right text-[14px] italic text-mist">“From messy data to a trusted customer view.”</p>
      </div>
      <Stepper steps={steps(s)} />
      <KpiRow k={k} withGolden={false} />
      <div className="grid grid-cols-[1fr_390px] gap-4">
        <section className="card flex flex-col px-6 py-5">
          <div className="flex items-start justify-between">
            <div className="flex items-start gap-3">
              <span className="grid h-10 w-10 place-items-center rounded-lg bg-tide/10 text-tide"><ListChecks className="h-5 w-5" /></span>
              <div>
                <h2 className="text-xl font-bold text-fog">Review matches</h2>
                <p className="text-[13px] text-mist">Review the pairs the matchers disagreed on and decide which to merge.</p>
              </div>
            </div>
            <div className="text-right">
              <div className="num text-[15px] font-semibold text-fog">{review == null ? "—" : review.toLocaleString("en-GB")} pairs</div>
              <div className="text-[11px] italic text-mist">sorted by risk (high → low)</div>
            </div>
          </div>
          <div className="flex flex-1 flex-col items-center justify-center py-8 text-center">
            <div className="flex items-center gap-4">
              <div className="grid h-20 w-28 place-items-center rounded-xl bg-tide/10"><UserRound className="h-8 w-8 text-tide" /></div>
              <div className="flex gap-1.5">{[0, 1, 2].map((i) => <span key={i} className="h-2 w-2 rounded-full bg-tide/30" />)}</div>
              <div className="grid h-20 w-28 place-items-center rounded-xl bg-sky/10"><UserRound className="h-8 w-8 text-sky" /></div>
            </div>
            <h3 className="mt-6 text-lg font-semibold text-fog">{review ? "Start reviewing matches" : "No pairs waiting for review"}</h3>
            <p className="mt-1 text-[13px] text-mist">Go through the suggested pairs, inspect the details and accept or reject each match.</p>
            <button onClick={onReview} className="mt-6 flex items-center gap-3 rounded-lg bg-tide px-8 py-3 text-[15px] font-semibold text-white shadow-sm hover:brightness-110">
              <ArrowRight className="h-5 w-5" /> Continue Review
              {review != null && <span className="ml-6 text-[12px] font-normal opacity-90">{review.toLocaleString("en-GB")} pairs</span>}
            </button>
            {canExample && (
              <button onClick={onExample} className="mt-3 flex items-center gap-2 text-[13px] font-medium text-tide hover:underline">
                <Eye className="h-4 w-4" /> Or view the illustrative example (invented numbers)
              </button>
            )}
          </div>
        </section>
        <section className="card flex flex-col px-5 py-4">
          <div className="flex items-center justify-between">
            <h2 className="flex items-center gap-2 text-[16px] font-bold text-fog"><Clock className="h-5 w-5" /> Recent activity</h2>
            <span className="text-[12px] text-mist">latest first</span>
          </div>
          <ol className="relative mt-4 flex-1 space-y-4 before:absolute before:bottom-2 before:left-[5px] before:top-2 before:w-px before:bg-rule">
            {activity(s, 6).map((n, i) => (
              <li key={i} className="relative flex gap-3 pl-6">
                <span className={`absolute left-0 top-1.5 h-[11px] w-[11px] rounded-full ${DOT[n.tone ?? "info"]}`} />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[13px] font-medium text-fog" title={n.text}>{n.text}</div>
                  <div className="text-[11px] uppercase tracking-wide text-mist">{n.kind}</div>
                </div>
                <span className="shrink-0 text-[11px] text-mist">{n.ts ? new Date(n.ts).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : ""}</span>
              </li>
            ))}
            {s.narrative.length === 0 && <li className="pl-6 text-[13px] text-mist">Nothing yet — start a run.</li>}
          </ol>
          <div className="mt-4 rounded-lg border border-rule px-4 py-3">
            <div className="flex items-center gap-3">
              <span className="grid h-9 w-9 place-items-center rounded-full bg-tide/10 text-tide"><Play className="h-4 w-4" /></span>
              <div className="min-w-0 flex-1">
                <div className="text-[12px] text-mist">Current run</div>
                <div className="truncate text-[14px] font-semibold text-fog">{run?.id ?? "—"}</div>
                <div className="text-[12px] text-mist">
                  {run ? `${run.mode} · started ${new Date(run.startedAt).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}` : "no run yet"}
                  {mins != null ? ` · ${mins}m elapsed` : ""}
                </div>
              </div>
              {run && <span className={`rounded-full px-2.5 py-1 text-[11px] font-medium ${run.ended ? "bg-kelp/10 text-kelp" : "bg-tide/10 text-tide"}`}>{run.ended ? "Completed" : "In Progress"}</span>}
            </div>
            <div className="mt-3 flex items-center gap-3">
              <div className="h-2 flex-1 rounded-full bg-deck"><div className="h-full rounded-full bg-tide" style={{ width: `${prog}%` }} /></div>
              <span className="num text-[13px] text-fog">{prog}%</span>
            </div>
          </div>
        </section>
      </div>
    </div>
  );
}
