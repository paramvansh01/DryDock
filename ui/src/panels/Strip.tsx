import { AnimatePresence, motion } from "framer-motion";
import { Check, Undo2, X } from "lucide-react";
import { useState } from "react";
import { api } from "../api";
import type { State } from "../types";
import { Chip, fmt, statusTone } from "../ui";

// One card per branch, accumulating as the agent works. Merged branches offer
// unmerge; the fingerprint tick appears only when unmerge.applied reports a match.
export function Strip({ s, focus, onFocus, canAct }: { s: State; focus: string | null; onFocus: (id: string) => void; canAct: boolean }) {
  const [busy, setBusy] = useState<string | null>(null);
  const unmerge = async (id: string, mergeId: number) => {
    setBusy(id);
    try { await api.unmerge(mergeId); } catch (e) { alert(String(e)); }
    setBusy(null);
  };
  return (
    <div className="scroll-thin flex gap-2 overflow-x-auto border-t border-rule bg-hull px-3 py-2">
      <span className="self-center pr-1 text-[11px] font-semibold uppercase tracking-[0.14em] text-mist">Branches</span>
      <AnimatePresence initial={false}>
        {s.order.map((id) => {
          const b = s.branches[id];
          const rows = b.diff ? b.diff.added + b.diff.changed + b.diff.deleted : null;
          return (
            <motion.button key={id} initial={{ x: 20 }} animate={{ x: 0 }}
              onClick={() => onFocus(id)}
              className={`min-w-44 shrink-0 rounded-md border px-2.5 py-1.5 text-left ${focus === id ? "border-tide" : "border-rule"} bg-deck/40`}>
              <div className="flex items-center gap-1.5">
                <span className="font-mono text-[11px] text-fog">{id}</span>
                <Chip tone={statusTone(b.status)}>{b.status}</Chip>
              </div>
              <div className="mt-0.5 text-[11px] text-mist">{b.cls || "—"}{rows != null ? ` · ${fmt(rows)} rows` : ""}</div>
              {b.status === "MERGED" && b.applied && (
                <span role="button" onClick={(e) => { e.stopPropagation(); if (canAct && busy !== id) unmerge(id, b.applied!.merge_id); }}
                  className={`mt-1 inline-flex items-center gap-1 text-[11px] ${canAct ? "text-sky hover:underline" : "text-mist"}`}><Undo2 className="h-3 w-3" />unmerge</span>
              )}
              {b.unmerged && (
                <div className={`mt-1 flex items-center gap-1 text-[11px] ${b.unmerged.fingerprint_match ? "text-kelp" : "text-flare"}`}>
                  {b.unmerged.fingerprint_match ? <><Check className="h-3 w-3" />fingerprint match · restored</> : <><X className="h-3 w-3" />fingerprint mismatch</>}
                </div>
              )}
            </motion.button>
          );
        })}
      </AnimatePresence>
    </div>
  );
}
