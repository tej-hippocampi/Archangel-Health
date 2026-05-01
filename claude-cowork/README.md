# Archangel Health — Claude Cowork Bundle

Drop-in context pack for spinning up a Claude Cowork project that thinks like
a world-class B2B healthcare PM + a health-system executive + a practicing
surgeon, oriented around **Archangel Health** and the **CMS TEAM** model.

## How to use in Claude Cowork

1. **Create a new project** in Claude Cowork named `Archangel Health — Product Brain` (or similar).
2. Open `SYSTEM_PROMPT.md`, copy the prompt block (everything below the `---`), and paste it into the project's **System Prompt / Custom Instructions** field.
3. Upload the four context files as **project knowledge / project files**:
   - `01_COMPANY_AND_PRODUCT_CONTEXT.md`
   - `02_TECHNICAL_CONTEXT.md`
   - `03_CLINICAL_AND_TEAM_CONTEXT.md`
   - `04_BRAINSTORM_KICKOFF.md`
4. Start a new chat. Open with a trigger + constraint + decision (see `04_BRAINSTORM_KICKOFF.md`).

## Files in this bundle

| File | What it is | Goes where |
|---|---|---|
| `SYSTEM_PROMPT.md` | The persona + operating principles + response shape | System prompt |
| `01_COMPANY_AND_PRODUCT_CONTEXT.md` | Mission, current product surface, ICP, gaps | Project knowledge |
| `02_TECHNICAL_CONTEXT.md` | Stack, repo layout, integrations, what's built vs. not | Project knowledge |
| `03_CLINICAL_AND_TEAM_CONTEXT.md` | TEAM model, episode-family clinical notes, economic levers, glossary | Project knowledge |
| `04_BRAINSTORM_KICKOFF.md` | How to run a brainstorm session, sample prompts, default response shape | Project knowledge |

## Refresh cadence

These files are a snapshot. Re-export them whenever:

- The product surface changes meaningfully (new module, new persona served).
- CMS publishes an update to the TEAM final rule.
- An ICP / GTM pivot happens.
- A major integration ships (Epic, FHIR, etc.).

The fastest way to refresh is to ask Claude Code in this repo:
*"Re-read the codebase and update `claude-cowork/01_COMPANY_AND_PRODUCT_CONTEXT.md` and `02_TECHNICAL_CONTEXT.md` to reflect what's actually shipped today. Don't change `03_*` (that's clinical / regulatory ground truth) unless I ask."*
