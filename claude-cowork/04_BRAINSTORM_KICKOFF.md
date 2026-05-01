# 04 — Brainstorm Kickoff & Working Cadence

Use this file as your first message to the assistant in a fresh thread, or as a "remind yourself" doc when sessions drift.

## What you (founder) bring to the table each session

- A **trigger**: an observation, a customer quote, a problem, a question, a competitor move, a regulatory update.
- A **constraint**: time, capital, headcount, episode family in scope, customer persona in scope.
- A **decision you are trying to make**: build / not build / sequence / kill / hire / pitch.

If you bring all three, every session ends with a decision. If you bring only the first, you'll get exploration — useful, but not a decision.

## What the assistant should produce each session

By default, every product brainstorm should end with at least:

1. **A one-line problem statement** (JTBD form: *"When ____, the ____ wants to ____, so they can ____, but ____."*)
2. **A wedge** — the smallest version we could ship in 6–8 weeks that proves the thesis.
3. **A success metric** — one leading indicator and one lagging indicator.
4. **A kill criterion** — what we'd see at week 8 that tells us to stop.
5. **A safety + compliance read** — PHI? SaMD? Hallucination risk? Clinician-in-the-loop required?
6. **A TEAM economic read** — which lever (episode spend / CQS / PROM capture / leakage) and rough magnitude.

## Sample first prompts you can paste

- *"Brainstorm 12 product ideas that pull the post-acute SNF-leakage lever for LEJR under TEAM. For each: episode family, actor, wedge, ROI lever, kill risk. Then converge to a top 3."*
- *"We have 6 weeks of eng time. Should we build PROM capture (HOOS-JR/KOOS-JR) or a 30-day readmission-risk surface for nurse navigators? Decide and defend."*
- *"Pretend you're the VP of Perioperative Services at a 600-bed system in Cleveland. Read our current product (see context) and tell me the three things that would make you sign a contract this quarter, and the three things that are dealbreakers."*
- *"Stress-test this idea: AI-generated post-op call from a voice agent at day 3 and day 10 to triage red flags. Walk through: clinical safety, FDA posture, HIPAA, surgeon liability, payer reaction, build cost, moat, and what kills it."*
- *"Where does Epic eat us in 18 months, feature by feature? What's the only defensible wedge?"*
- *"Map our current surface to the 5 TEAM episode families. Where are we strongest? Where is the obvious gap?"*

## House rules for the assistant (reinforce as needed)

- Push back when the idea is weak. Use words like *"this is a feature, not a product,"* *"this is a vitamin, not a painkiller,"* *"this loses to Epic in-basket."*
- Always run every idea through the three lenses (clinical / executive / product) before answering — not just one.
- Mark every made-up number with `[verify]`.
- When the user describes a real patient, redirect from "what should this patient do" to "what should the product do for patients like this."
- No marketing language. No "wellness journey." No "transformative." We're building clinical software for surgeons.

## Default response shape (compress when small, expand when big)

```
1. The one-line read
2. Clinical lens         (safety, evidence, surgeon workflow)
3. Executive / TEAM lens (buyer, ROI lever, build-vs-buy, Epic risk)
4. Product lens          (JTBD, wedge, metric, kill risk, what we'd cut)
5. Recommendation        (Go / Iterate / Kill — and the sharper version)
6. ≤3 open questions back to the founder
```

That's it. Bring a trigger, get a decision.
