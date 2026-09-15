// The guided tour: a spotlight on one part of the screen at a time, with a sentence or two about what it is for.
// Steps name their target with a data-tour attribute and, when needed, the tab it lives on; a step whose target
// is not on screen (no merge waiting yet, say) still shows its text, in the middle of the page.

import { ArrowLeft, ArrowRight, Sparkles, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import type { Tab } from "../components/Shell";

export interface TourStep { tab?: Tab; target?: string; title: string; body: string }

export const TOUR: TourStep[] = [
  { title: "Welcome to Drydock",
    body: "Drydock lets software, such as an AI agent, change your customer list safely. It works on a copy, shows you exactly which rows " +
          "would change, and nothing reaches the real list until you approve it. Everything can be undone. This tour takes about two minutes." },
  { tab: "data", target: "data-now", title: "What Drydock is working on",
    body: "Your two files (System A and System B), or the demo data. System A is the list you trust most; the clean list starts from it." },
  { tab: "data", target: "data-ready", title: "Ready to run?",
    body: "Before it changes anything, Drydock checks the database and itself. If a line here turns red, the fix is written right next to it." },
  { tab: "data", target: "data-wizard", title: "Bring your own data",
    body: "Upload two CSV files, check which column is which, and read a quality report before anything is loaded. No files? Download our samples." },
  { tab: "reconcile", target: "start-run", title: "Start a run",
    body: "This is where the work begins. Choose 'scripted' and press Start: Drydock looks for customers who appear in both lists, or twice in one." },
  { tab: "reconcile", target: "stepper", title: "Progress",
    body: "The steps light up as a run goes: finding candidates, matching, your review, the exact changes, and merging." },
  { tab: "reconcile", target: "kpis", title: "Three matchers vote",
    body: "Every likely pair gets three independent votes. All three agree: auto-matched. All three say no: rejected. They disagree: a person decides." },
  { tab: "reconcile", target: "candidates", title: "Every pair, side by side",
    body: "Click a row to see both records and the clues for and against. The filter can show only the 'Split votes', the ones that need you." },
  { tab: "diff", target: "branches-strip", title: "Copies, not the real list",
    body: "Each card is a branch: a full copy of your clean list with one kind of change made to it. The real list is untouched while you look." },
  { tab: "diff", target: "diff-panel", title: "The exact rows that would change",
    body: "Not a prediction: Drydock compares the copy with the real list row by row. Blue is added, amber changed, red deleted. Untick any row you don't want." },
  { tab: "gate", target: "gate-list", title: "The Merge Gate",
    body: "Small, safe changes merge by themselves. Riskier ones wait here, and each one says why, in plain words." },
  { tab: "gate", target: "gate-panel", title: "Approve, reject, or sign off first",
    body: "MERGE puts only the ticked rows into the real list, all at once. 'Download changes' gives you every affected row as a spreadsheet for sign-off first." },
  { tab: "diff", target: "branches-strip", title: "Undo, exactly",
    body: "Changed your mind? 'unmerge' on a merged card puts the list back exactly as it was, and a fingerprint proves it. Undo the newest change first." },
  { tab: "runs", target: "audit-download", title: "The audit log",
    body: "Every decision, who made it, when and why, with fingerprints before and after. Download it whenever someone asks what changed." },
  { tab: "database", target: "db-tree", title: "Look inside",
    body: "Every table: your two files, the clean list and each copy. Open GOLDEN · CUSTOMERS and click a customer to see their history." },
  { tab: "database", target: "db-lookup", title: "One customer's full story",
    body: "Type a clean-list id to see where each detail came from, the source records behind it, why they were linked, and every change." },
  { tab: "database", target: "db-download", title: "Take the clean list with you",
    body: "Download the reconciled list as a CSV at any time." },
  { tab: "system", target: "system-exasol", title: "Live System",
    body: "A live map of how everything connects. The numbers come straight from the database; press Refresh to read them again." },
  { target: "reviewer", title: "Your name",
    body: "Set your name here, so the audit log shows who approved, rejected or undid each change." },
  { target: "help", title: "That's the tour",
    body: "Press ? any time to see it again. Next: load your data (or use the demo) and start a run." },
];

const PAD = 8;
const CARD_W = 380;

export function Tour({ open, onClose, setTab }: { open: boolean; onClose: () => void; setTab: (t: Tab) => void }) {
  const [i, setI] = useState(0);
  const [rect, setRect] = useState<DOMRect | null>(null);
  const step = TOUR[i];

  useEffect(() => { if (open) setI(0); }, [open]);

  const measure = useCallback(() => {
    if (!step?.target) { setRect(null); return false; }
    const el = document.querySelector(`[data-tour="${step.target}"]`) as HTMLElement | null;
    if (!el) { setRect(null); return false; }
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) { setRect(null); return false; }
    setRect(r);
    return true;
  }, [step]);

  useLayoutEffect(() => {
    if (!open || !step) return;
    if (step.tab) setTab(step.tab);
    setRect(null);
    let tries = 0;
    const id = window.setInterval(() => {         // the tab may still be rendering: look for the target a few times
      const el = step.target ? document.querySelector(`[data-tour="${step.target}"]`) as HTMLElement | null : null;
      if (el) {
        const r = el.getBoundingClientRect();
        if (r.top < 70 || r.bottom > window.innerHeight - 20) el.scrollIntoView({ block: "center", behavior: "smooth" });
        window.setTimeout(measure, 320);
        window.clearInterval(id);
      } else if (++tries > 12 || !step.target) {
        window.clearInterval(id);
      }
    }, 100);
    return () => window.clearInterval(id);
  }, [open, i]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!open) return;
    const on = () => measure();
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
      if (e.key === "ArrowRight") setI((x) => Math.min(TOUR.length - 1, x + 1));
      if (e.key === "ArrowLeft") setI((x) => Math.max(0, x - 1));
    };
    window.addEventListener("resize", on);
    window.addEventListener("scroll", on, true);
    window.addEventListener("keydown", key);
    return () => { window.removeEventListener("resize", on); window.removeEventListener("scroll", on, true); window.removeEventListener("keydown", key); };
  }, [open, measure, onClose]);

  if (!open || !step) return null;
  const last = i === TOUR.length - 1;
  const vw = window.innerWidth, vh = window.innerHeight;
  let card: React.CSSProperties = { left: (vw - CARD_W) / 2, top: vh / 2 - 120 };
  if (rect) {
    const below = vh - rect.bottom > 230;
    const left = Math.min(Math.max(16, rect.left + rect.width / 2 - CARD_W / 2), vw - CARD_W - 16);
    card = below ? { left, top: rect.bottom + PAD + 12 } : { left, top: Math.max(16, rect.top - PAD - 12 - 210) };
  }

  return (
    <div className="fixed inset-0 z-[60]" role="dialog" aria-modal="true" aria-label="Guided tour">
      {rect ? (
        <div className="pointer-events-none fixed rounded-xl transition-all duration-300 ease-out"
          style={{ left: rect.left - PAD, top: rect.top - PAD, width: rect.width + PAD * 2, height: rect.height + PAD * 2,
                   boxShadow: "0 0 0 3px rgba(37,99,235,.9), 0 0 0 9999px rgba(15,23,42,.55)" }} />
      ) : (
        <div className="fixed inset-0 bg-fog/55" />
      )}
      <div className="card fixed px-5 py-4 shadow-2xl transition-all duration-300 ease-out" style={{ ...card, width: CARD_W }}>
        <div className="flex items-center gap-2">
          <Sparkles className="h-4 w-4 text-tide" />
          <span className="text-[11px] font-semibold uppercase tracking-[0.14em] text-mist">Tour · {i + 1} of {TOUR.length}</span>
          <button onClick={onClose} className="ml-auto text-mist hover:text-fog" aria-label="Close the tour"><X className="h-4 w-4" /></button>
        </div>
        <h3 className="mt-1.5 text-[16px] font-bold text-fog">{step.title}</h3>
        <p className="mt-1 text-[13.5px] leading-relaxed text-fog/85">{step.body}</p>
        <div className="mt-3 flex items-center gap-1">
          {TOUR.map((_, k) => (
            <button key={k} onClick={() => setI(k)} aria-label={`Step ${k + 1}`}
              className={`h-1.5 rounded-full transition-all ${k === i ? "w-5 bg-tide" : "w-1.5 bg-rule hover:bg-mist"}`} />
          ))}
        </div>
        <div className="mt-3 flex items-center gap-2">
          <button onClick={onClose} className="text-[12.5px] text-mist hover:text-fog">Skip tour</button>
          <button onClick={() => setI((x) => Math.max(0, x - 1))} disabled={i === 0}
            className="ml-auto flex items-center gap-1 rounded-lg border border-rule px-3 py-1.5 text-[13px] disabled:opacity-40">
            <ArrowLeft className="h-3.5 w-3.5" /> Back
          </button>
          <button onClick={() => (last ? onClose() : setI((x) => x + 1))}
            className="flex items-center gap-1 rounded-lg bg-navy px-4 py-1.5 text-[13px] font-semibold text-white">
            {last ? "Finish" : <>Next <ArrowRight className="h-3.5 w-3.5" /></>}
          </button>
        </div>
      </div>
    </div>
  );
}

/** Shown right after a person loads their own files: the tour is offered, never forced. */
export function TourOffer({ onYes, onNo }: { onYes: () => void; onNo: () => void }) {
  return (
    <div className="fixed inset-0 z-[55] grid place-items-center bg-fog/30">
      <div className="card w-[440px] px-6 py-5 text-center shadow-2xl">
        <Sparkles className="mx-auto h-8 w-8 text-tide" />
        <h2 className="mt-2 text-lg font-bold text-fog">Your data is loaded</h2>
        <p className="mt-1 text-[13.5px] text-fog/80">Would you like a quick tour? It shows where everything is and what each important button does, in about two minutes.</p>
        <div className="mt-4 flex justify-center gap-2">
          <button onClick={onNo} className="rounded-lg border border-rule px-4 py-2 text-[13px]">No thanks</button>
          <button onClick={onYes} className="rounded-lg bg-navy px-5 py-2 text-[13px] font-semibold text-white">Yes, show me around</button>
        </div>
      </div>
    </div>
  );
}
