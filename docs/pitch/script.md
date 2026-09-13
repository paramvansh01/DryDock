# Drydock: 3-minute pitch script

About 400 spoken words, roughly 2 min 45 s at a natural, conversational pace. The slides are in
[Drydock-Pitch-Deck.pdf](Drydock-Pitch-Deck.pdf). Every number comes from a run on
Exasol Personal 2026.2; a fresh run gives slightly different figures, so read them off the screen.

| Time | SHOW (screen + action) | SAY (word for word) |
|---|---|---|
| **0:00–0:24** Problem | Title card "DRYDOCK — safe writes for AI agents", then the **Overview** tab. | "So, picture this. A bank buys a smaller one — two customer databases, the same people in both, spelled differently. Someone asks an AI agent to merge them. It writes the SQL, it runs, and a father and son at one address become a single customer. One of them loses his loan history, and nobody notices for weeks, because nobody could see which rows would change before it ran." |
| **0:24–0:44** Solution | **Live System** tab. Trace with the cursor: *Drydock MCP → Branches → Orchestrator's merge gate → GOLDEN*. | "So that's the gap. Drydock is a governed write path for AI agents on Exasol — and it's why Exasol's own MCP server ships with writes switched off. The agent never touches production. It works on a branch, a real copy inside Exasol. A person then sees the exact rows that would change, approves them row by row, and any merge can be undone exactly." |
| **0:44–1:07** Live run | *Pre-recorded, sped up:* **Start New Run** (scripted, gate on, tier 2) with **Live System** open in **Slow motion**: dots labelled "copy-on-write · 36,600 rows", "row-level diff · … changed", "gate held … for a person". | "Here it's reconciling two messy customer systems: 69,000 records, with 400 traps planted in them — fathers and sons, spouses sharing a landline, people versus their own company. A scripted playbook drives it, so the run is reproducible. Everything moving is a real event: Exasol copies production in about a second, hashes every row for the exact diff, and the gate decides." |
| **1:07–1:28** Matching + AI | **Reconcile** → filter **Split votes** → open a pair. Hover the three dots, then the **Gemini** verdict pill (tooltip: "saw a comparison summary only"). | "Now, three matchers vote on every pair, in SQL, inside Exasol. They settled 98.5% of 30,409 pairs on their own. Only the 461 they disagreed on went to Gemini — and Gemini never sees a name or an email, just a comparison summary. Even then it's only advice: those rows still wait for a person." |
| **1:28–2:05** Gate, proof, undo | **Merge Gate**: click through the held requests and their reasons (split rows; *DELETE_PCT 1.46% > 1.0%*). **Diff Viewer**: split rows unticked, untick one more → **MERGE N OF M**. *Split screen:* Exasol console `SELECT COUNT(*) FROM GOLDEN.CUSTOMERS;` before and after merging INTERNAL_DEDUP → **unmerge** → count restored, **fingerprint match**. | "So the agent proposed around 28,000 merges. The gate let the safe batch through and held the rest — these because the matchers split, this one because it would delete 1.46% of production, where the tier allows one. I review, untick what I don't like, and merge: staged, fingerprint-checked, swapped in atomically. And there's Exasol's own console agreeing with the count. Undo is exact too — the fingerprint matches, byte for byte." |
| **2:05–2:27** Backend | **Live System**: click the **Exasol** box (inspector: version, session, tiers from TIER_CONFIG), then the **Orchestrator** box. Optional 2-s cut to the terminal showing the live tests passing. | "Underneath, Exasol does the heavy lifting: the copies, SHA-256 row hashing, matching with EDIT_DISTANCE and SOUNDEX, transactional renames. A Python orchestrator hosts the gate. Agents connect over MCP — Exasol's official server for reads, ours for writes — and Gemini 3.5 Flash adjudicates. Every statement passes a dialect firewall, and 38 tests prove it on the live instance." |
| **2:27–2:42** Results | **Runs** scoreboard, or the README's *Results on the live instance* table. | "And the numbers: 27,980 of 27,981 proposed merges correct, 99.9% recall, all 600 duplicates found — measured against a ground truth the agent never sees." |
| **2:42–2:52** Why Exasol, close | Wide shot of **Live System**, then the title card. | "So that's the idea: Exasol is fast enough that branching production for every AI change becomes the default. Drydock — governed changes, observed impact." |

## Likely questions

- **"Isn't a full copy per branch expensive?"** "No: a full copy of the customer table takes about a second on a laptop,
  and an engine-level clone would make it instant at any scale."
- **"What stops the agent writing to production?"** "Exasol does: the agent's user has no write grant anywhere. Fifteen
  forbidden actions are tested live, and GOLDEN is written by exactly one module."
- **"Why an LLM at all?"** "Only for the ambiguous 1.5%, as advice. The 98.5% is plain SQL, and every split row
  still waits for a person."
- **"How do we know the numbers are real?"** "Open Live System and press Refresh — that's Exasol's own session and
  row counts, right now. Or run `SELECT COUNT(*)` in the Exasol console, as in the video."
