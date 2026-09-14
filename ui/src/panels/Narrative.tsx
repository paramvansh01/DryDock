import { useEffect, useRef } from "react";
import type { State } from "../types";
import { Panel, clock, fmt } from "../ui";

const TONE: Record<string, string> = {
  info: "border-rule", warn: "border-brass", good: "border-kelp", bad: "border-flare", disclose: "border-sky",
};

export function Narrative({ s, onFocus }: { s: State; onFocus: (id: string) => void }) {
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [s.narrative.length]);
  const p = s.panel;
  const cand = s.candidates?.find((c) => c.kind === "AB");
  return (
    <Panel title="Agent narrative" className="h-full">
      <div className="flex h-full flex-col">
        {(p || cand) && (
          <div className="shrink-0 border-b border-rule px-3 py-2 text-xs">
            <div className="text-[10px] uppercase tracking-wider text-mist">review burden</div>
            <div className="num mt-0.5 text-fog">
              {cand ? fmt(cand.candidates) : "—"} candidates → {p ? fmt(p.unanimous_merge + p.split) : "—"} merges →{" "}
              <span className="font-semibold text-brass">{p ? fmt(p.split) : "—"} disagreements</span>
            </div>
            {cand && <div className="mt-0.5 text-mist">{fmt(cand.possible_pairs)} possible pairs · {(cand.ms / 1000).toFixed(1)}s</div>}
          </div>
        )}
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-3 py-2">
          <ol className="space-y-1.5">
            {s.narrative.map((n, i) => (
              <li key={i} className={`border-l-2 pl-2 text-[12.5px] leading-snug ${TONE[n.tone ?? "info"]}`}>
                <span className="mr-1.5 font-mono text-[10px] text-mist">{clock(n.ts)}</span>
                <span className="mr-1 text-[10px] uppercase tracking-wide text-mist">{n.kind}</span>
                {n.branchId ? (
                  <button className="text-left text-fog hover:text-tide" onClick={() => onFocus(n.branchId!)}>{n.text}</button>
                ) : <span className="text-fog">{n.text}</span>}
              </li>
            ))}
          </ol>
          <div ref={end} />
        </div>
      </div>
    </Panel>
  );
}
