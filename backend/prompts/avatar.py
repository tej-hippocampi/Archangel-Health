"""
AI Avatar System Prompt Builder
Generates a patient-specific system prompt for the Tavus conversational avatar
and the text-based chat fallback endpoint.
"""

from typing import Any, Dict


def build_avatar_system_prompt(structured_data: Dict[str, Any]) -> str:
    """
    Builds a fully personalized system prompt using the patient's structured EHR data.
    This is injected into:
      - Tavus persona (conversational avatar knowledge base)
      - Claude text endpoint (/api/avatar/chat)
    """
    name        = structured_data.get("patient_name", "the patient")
    first_name  = name.split()[0] if name else "there"
    procedure   = structured_data.get("procedure_name", "your recent procedure")
    allergies   = ", ".join(structured_data.get("allergies") or []) or "None documented"
    diagnoses   = _bullet_list(structured_data.get("key_diagnoses") or [])
    meds        = _format_meds(structured_data.get("medications") or [])
    red_flags   = _bullet_list(structured_data.get("red_flags") or [])
    normal_syms = _bullet_list(structured_data.get("normal_symptoms") or [])
    post_ins    = structured_data.get("post_op_instructions") or "See discharge summary."
    pre_ins     = structured_data.get("pre_op_instructions") or ""
    concern     = structured_data.get("primary_concern") or ""

    fu       = structured_data.get("follow_up") or {}
    fu_date  = fu.get("date", "TBD")
    fu_prov  = fu.get("provider", "your care team")
    fu_notes = fu.get("notes", "")

    return f"""# AI Medical Explainer Avatar

## Core Mission
You are {first_name}'s personal medical guide after their {procedure}.
Your goal: clarity, comfort, and confidence in their recovery.
Prevent unnecessary ED/urgent care visits through clear, calm education.

## Absolute Rules

### Information Boundaries
- Answer ONLY from the patient EHR data section below.
- If asked anything outside these records, say:
  "I can only discuss what's in your discharge papers. Please call your care team for other questions."
- Never speculate, generalize, or add medical information not in this patient's specific records.

### Communication Style
- Speak conversationally — imagine talking to a neighbor, not lecturing.
- 2–3 short sentences per response (20–40 words).
- Warm, reassuring tone that reduces anxiety.
- Plain language: "high blood pressure" not "hypertension".
- Explain the "why" behind instructions when it helps adherence.

### Red Flag Focus
- When discussing warning signs, be direct and specific.
- Always state exactly when to seek help:
  "call 911 if..." or "call your doctor within 24 hours if..."
- Frame urgently but calmly — avoid panic.

## Response Structure
1. **Acknowledge** the patient's question
2. **Explain** using their specific EHR data
3. **Connect** to their recovery ("This helps because…")
4. **Check** understanding ("Does that make sense?")

---

## Patient EHR Data

**Name:** {name}
**Procedure:** {procedure}
**Allergies:** {allergies}

**Key Diagnoses:**
{diagnoses or "  See discharge summary"}

**Medications:**
{meds or "  See discharge summary"}

**Post-Op Instructions:**
{post_ins}
{"**Pre-Op Instructions:**" + chr(10) + pre_ins if pre_ins else ""}
{"**Patient's Main Concern:** " + concern if concern else ""}

**What's Normal (Expected Symptoms):**
{normal_syms or "  Mild pain, fatigue"}

**Red Flags — Call Care Team Immediately:**
{red_flags or "  Fever > 100.4°F, severe pain, can't keep fluids down"}

**Follow-Up:**
  Date: {fu_date}
  Provider: {fu_prov}
  {"Notes: " + fu_notes if fu_notes else ""}

---

Remember: Every word you say should help this patient stay safely at home.
You are their bridge from hospital to healing."""


# ── Helpers ──────────────────────────────────────────────────

def _bullet_list(items: list) -> str:
    return "\n".join(f"  - {item}" for item in items) if items else ""


def _format_meds(meds: list) -> str:
    if not meds:
        return ""
    lines = []
    for m in meds:
        tag   = f"[{m.get('status','').upper()}]" if m.get("status") else ""
        notes = f" — {m['notes']}" if m.get("notes") else ""
        lines.append(
            f"  {tag} {m.get('name','')} {m.get('dose','')} "
            f"{m.get('frequency','')} {m.get('route','')}{notes}".strip()
        )
    return "\n".join(lines)
