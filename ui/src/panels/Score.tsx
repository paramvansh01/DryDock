import type { State } from "../types";
import { Panel } from "../ui";

// Six numbers, no chart. Rows come only from score.updated events (bench/score.py);
// nothing here is typed in. Precision/recall/F1 are the matcher's; the last column is the gate's.
export function Score({ s }: { s: State }) {
  const rows = Object.entries(s.scores).map(([runId, sc]) => {
    const run = s.runs[runId];
    const label = run ? (run.gateEnabled ? "gated" : "control") : runId;
    return { runId, label, sc };
  });
  return (
    <Panel title="A/B scoreboard" right={s.illustrative ? <span className="text-[10px] text-brass">illustrative</span> : null}>
      {rows.length === 0 ? (
        <p className="px-3 py-3 text-xs text-mist">
          {s.system.snapshot?.config.dataset?.scored === false
            ? "Scores need an answer key, and only the demo data has one. For your own data, your review is the check."
            : "No scored run yet. Scores come from bench/score.py at the end of a run."}
        </p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-[10px] uppercase tracking-wider text-mist">
              <th className="px-3 py-1.5 text-left font-medium">run</th>
              <th className="px-2 text-right font-medium">P</th>
              <th className="px-2 text-right font-medium">R</th>
              <th className="px-2 text-right font-medium">F1</th>
              <th className="px-3 text-right font-medium" title="false merges that reached GOLDEN">false merges</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ runId, label, sc }) => (
              <tr key={runId} className="border-t border-rule">
                <td className="px-3 py-1.5" title={runId}><span className="font-medium text-fog">{label}</span></td>
                <td className="num px-2 text-right">{sc.precision.toFixed(3)}</td>
                <td className="num px-2 text-right">{sc.recall.toFixed(3)}</td>
                <td className="num px-2 text-right">{sc.f1.toFixed(3)}</td>
                <td className={`num px-3 text-right text-base font-semibold ${sc.false_merges_in_golden ? "text-flare" : "text-kelp"}`}>
                  {sc.false_merges_in_golden}
                  {sc.decoy_false_merges ? <div className="text-[10px] font-normal text-mist">{sc.decoy_false_merges} decoys</div> : null}
                </td>
              </tr>
            ))}
            <tr className="border-t border-rule text-mist">
              <td className="px-3 py-1.5 whitespace-nowrap">DBLP–ACM</td>
              <td colSpan={4} className="px-3 text-right text-xs">not run</td>
            </tr>
          </tbody>
        </table>
      )}
    </Panel>
  );
}
