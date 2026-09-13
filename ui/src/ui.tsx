import { animate, motion, useMotionValue, useTransform } from "framer-motion";
import { Check, CircleHelp, Sparkles, X } from "lucide-react";
import { useEffect } from "react";
import { adjudicatorInfo } from "./derive";

export const fmt = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString("en-GB"));
export const short = (fp: string | null | undefined) => (fp ? `${fp.slice(0, 4)}…${fp.slice(-4)}` : "—");
export const clock = (ts: string) => (ts ? new Date(ts).toLocaleTimeString("en-GB", { hour12: false }) : "");

/** A number that ticks up to its value when it changes. */
export function CountUp({ value, className = "" }: { value: number; className?: string }) {
  const mv = useMotionValue(0);
  const text = useTransform(mv, (v) => Math.round(v).toLocaleString("en-GB"));
  useEffect(() => {
    const c = animate(mv, value, { duration: Math.min(1.6, 0.4 + Math.log10(value + 1) * 0.3), ease: "easeOut" });
    return c.stop;
  }, [value, mv]);
  return <motion.span className={`num ${className}`}>{text}</motion.span>;
}

const TONES: Record<string, string> = {
  tide: "bg-tide/15 text-tide border-tide/40",
  brass: "bg-brass/15 text-brass border-brass/40",
  flare: "bg-flare/15 text-flare border-flare/40",
  kelp: "bg-kelp/15 text-kelp border-kelp/40",
  sky: "bg-sky/15 text-sky border-sky/40",
  mist: "bg-deck text-mist border-rule",
};

export function Chip({ tone = "mist", children, title }: { tone?: keyof typeof TONES; children: React.ReactNode; title?: string }) {
  return (
    <span title={title} className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide ${TONES[tone]}`}>
      {children}
    </span>
  );
}

export function statusTone(s: string): keyof typeof TONES {
  if (s === "MERGED" || s === "MERGE") return "kelp";
  if (s === "PENDING") return "brass";
  if (s === "BLOCKED" || s === "REJECTED") return "flare";
  if (s === "UNMERGED") return "sky";
  if (s === "OPEN") return "tide";
  return "mist";
}

export function Panel({ title, right, children, className = "" }: { title: React.ReactNode; right?: React.ReactNode; children: React.ReactNode; className?: string }) {
  return (
    <section className={`flex min-h-0 flex-col rounded-lg border border-rule bg-hull ${className}`}>
      <header className="flex items-center justify-between gap-3 border-b border-rule px-3 py-2">
        <h2 className="text-[11px] font-semibold uppercase tracking-[0.14em] text-mist">{title}</h2>
        {right}
      </header>
      <div className="min-h-0 flex-1">{children}</div>
    </section>
  );
}

// ------------------------------------------------------------------ the panel's vote and the adjudicator's verdict

const MATCHERS = [["deterministic", "Deterministic"], ["probabilistic", "Probabilistic"], ["skeptic", "Skeptic"]] as const;

/** Three matchers as three dots: green = match, red = no match. Hover names each one. */
export function PanelVotes({ votes, split }: { votes: Record<string, string>; split: boolean }) {
  const yes = MATCHERS.filter(([k]) => votes[k] === "MERGE").length;
  const title = MATCHERS.map(([k, l]) => `${l}: ${votes[k] === "MERGE" ? "match" : "no match"}`).join("\n");
  const text = split ? `Split ${yes}–${3 - yes}` : yes === 3 ? "All 3 agree" : "All 3 say no";
  return (
    <span title={title} className="inline-flex items-center gap-2 rounded-full border border-rule bg-hull py-1 pl-2.5 pr-3 text-[12px] leading-none">
      <span className="flex items-center gap-[3px]" aria-hidden>
        {MATCHERS.map(([k]) => (
          <span key={k} className={`h-2 w-2 rounded-full ${votes[k] === "MERGE" ? "bg-kelp" : "bg-flare/80"}`} />
        ))}
      </span>
      <span className={split ? "font-medium text-brass" : "text-mist"}>{text}</span>
    </span>
  );
}

const VERDICTS: Record<string, { text: string; Icon: typeof Check; cls: string }> = {
  SAME_PERSON: { text: "Same person", Icon: Check, cls: "border-kelp/25 bg-kelp/5 text-kelp" },
  DIFFERENT_PERSON: { text: "Different people", Icon: X, cls: "border-flare/25 bg-flare/5 text-flare" },
  INSUFFICIENT_EVIDENCE: { text: "Unsure", Icon: CircleHelp, cls: "border-brass/25 bg-brass/5 text-brass" },
};

/** The adjudicator's verdict, its confidence, and — always shown — who adjudicated. Hover for the reasoning. */
export function Verdict({ verdict, score, adjudicator, rationale }:
  { verdict: string; score: number | null; adjudicator: string | null; rationale?: string | null }) {
  const v = VERDICTS[verdict] ?? VERDICTS.INSUFFICIENT_EVIDENCE;
  const who = adjudicatorInfo(adjudicator);
  const tip = [who?.detail, rationale ? `Reason: ${rationale}` : null].filter(Boolean).join("\n\n");
  return (
    <span title={tip} className={`inline-flex items-stretch overflow-hidden rounded-full border text-[12px] leading-none ${v.cls}`}>
      <span className="inline-flex items-center gap-1.5 py-1 pl-2.5 pr-2.5 font-medium">
        <v.Icon className="h-3.5 w-3.5" strokeWidth={2.5} aria-hidden />
        {v.text}
        {score != null && <span className="num font-normal opacity-70">{Math.round(score * 100)}%</span>}
      </span>
      {who && (
        <span className={`inline-flex items-center gap-1 border-l border-rule bg-hull py-1 pl-2 pr-2.5 ${who.unavailable ? "text-brass" : "text-mist"}`}>
          <Sparkles className="h-3 w-3" aria-hidden />{who.label}
        </span>
      )}
    </span>
  );
}
