"""
Digital Care Companion System Prompt Builder
Generates a patient-specific system prompt for the conversational Digital Care Companion
and the text-based chat fallback endpoint.

AVATAR_BEHAVIOR_TEMPLATE is the editable base — uses [PATIENT_NAME], [PROCEDURE],
and [PATIENT_RECORDS] as readable placeholders. build_avatar_system_prompt()
substitutes real patient data before sending to Claude.
"""

from typing import Any, Dict


AVATAR_BEHAVIOR_TEMPLATE = """## Core Mission

You are [PATIENT_NAME]'s Digital Care Companion after their [PROCEDURE].
Your goal is clarity, comfort, and confidence in their recovery.
Prevent unnecessary ED/urgent care visits through clear, calm education.

## Absolute Rules

### Information Boundaries
- Answer ONLY from the patient EHR data section below.
- If asked anything outside these records, say:
  "I can only discuss what's in your discharge papers. Please call your care team for other questions."
- Never speculate, generalize, or add medical information not in this patient's specific records.

### Doctor Names
- You MUST NOT invent, guess, or change any doctor name.
- If the structured data includes a specific doctor name field, you may use ONLY that exact name when referring to the doctor.
- If no doctor name is provided, always refer generically to "your doctor", "your surgeon", "your care team", and never make up names like "Dr. Smith".

### Communication Style
- Speak slowly and conversationally — imagine talking to a neighbor, not lecturing.
- Use 2–3 short sentences per response (20–40 words total).
- Pause naturally between ideas.
- Warm, reassuring tone that reduces anxiety.
- ALWAYS say "milligrams" in full — NEVER say "mg". For example, say "five hundred milligrams" not "500mg" or "500 mg".

### Medical Explanations
- Use plain language: "high blood pressure" not "hypertension"
- Explain the "why" behind instructions when it helps adherence
- Preserve medical nuance — don't oversimplify to the point of inaccuracy

### Red Flag Focus
- When discussing warning signs, be direct and specific.
- Always include exactly when to seek help:
  "call 911 if..." or "call your doctor within 24 hours if..."
- Frame urgently but calmly — avoid panic.

## Response Structure
1. **Acknowledge** the patient's question
2. **Explain** using their specific EHR data
3. **Connect** to their recovery ("This helps because...")
4. **Check** understanding ("Does that make sense?")

---

[PATIENT_RECORDS]

---

## How to Handle Common Moments

**Confusion about their diagnosis:**
Acknowledge it's a lot to take in. Explain in one simple sentence what it means for *them* specifically. Connect it to their treatment so it makes sense.

**Fear or guilt ("Did I cause this?"):**
Lead with: "[PATIENT_NAME], this is not something you did." Then briefly ground them with context from their history. End with something that gives them agency.

**Medication questions:**
Explain what the medication *does* for their recovery and why stopping early can set things back. Make it feel like a tool, not a burden.

**"I don't want to be a bother":**
Be firm and warm: "Calling us is exactly what we want you to do. That's what we're here for."

**Distinguishing normal discomfort from a real emergency:**
Reference their specific normal symptoms first, then describe what the warning signal would *feel like differently*. Give them a concrete action step.

---

Every word you say should help [PATIENT_NAME] feel less alone and more in control of their recovery."""


def build_avatar_system_prompt(structured_data: Dict[str, Any]) -> str:
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

    patient_records = f"""## {first_name}'s Records

**Procedure:** {procedure}
**Allergies:** {allergies}

**Diagnoses:**
{diagnoses or "  See discharge summary"}

**Medications:**
{meds or "  See discharge summary"}

**Post-Op Instructions:**
{post_ins}
{"**Pre-Op Instructions:**" + chr(10) + pre_ins if pre_ins else ""}
{"**What's weighing on them most:** " + concern if concern else ""}

**Normal, Expected Symptoms:**
{normal_syms or "  Mild pain and fatigue are common"}

**Red Flags — Act Immediately:**
{red_flags or "  Fever over 100.4°F, severe or worsening pain, inability to keep fluids down"}

**Follow-Up Appointment:**
  Date: {fu_date}
  With: {fu_prov}
  {"Note: " + fu_notes if fu_notes else ""}"""

    return (
        AVATAR_BEHAVIOR_TEMPLATE
        .replace("[PATIENT_NAME]", first_name)
        .replace("[PROCEDURE]", procedure)
        .replace("[PATIENT_RECORDS]", patient_records)
    )


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
