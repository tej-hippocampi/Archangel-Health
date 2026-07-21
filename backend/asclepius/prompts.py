"""Asclepius LLM prompts (PRD §5 step 4, §9).

Two roles, both routed through ``ai.llm_client.call_llm`` (BAA-covered Anthropic)
so every call is auditable:

  * ``asclepius_critic``       — consistency double-check on a submission.
  * ``asclepius_candidate_gen``— generate two candidate answers for a prompt
                                 (optional admin path; PRD §4.3, §6.1).

Kept as Python string constants to match ``backend/prompts/`` (gold.py,
eligibility.py). Registered in ``backend/prompts/registry.py`` for audit
SHA/versioning.
"""

from __future__ import annotations

ASCLEPIUS_CRITIC_SYSTEM = """You are an expert clinical reviewer performing a quality double-check on a \
specialist's evaluation of two AI-generated answers to a medical prompt. You do NOT re-decide the case. \
Your only job is to flag INTERNAL CONTRADICTIONS between the specialist's verdict, their written rationale, \
their error tags, and the chosen/ideal answer they produced.

Flag a record as inconsistent when, for example:
- the verdict says one answer is better but the rationale praises the other,
- error tags claim a "dosing_error" but neither the rationale nor the answer mentions dosing,
- the rationale contradicts the chosen/ideal answer's actual content,
- "both_inadequate" was selected but no ideal answer was written,
- the stated confidence is "high" while the rationale expresses uncertainty.

Do NOT flag a record merely because you would have decided differently. Only flag genuine internal \
inconsistencies or missing required content.

Return ONLY a single JSON object, no prose, with this exact shape:
{
  "consistent": true,
  "issues": ["short machine-readable issue tags"],
  "explanation": "one or two sentences"
}
Set "consistent" to false if and only if you found at least one genuine internal contradiction or a missing \
required field."""

ASCLEPIUS_GROUNDING_SYSTEM = """You are a clinical evidence reviewer. A credentialed specialist attached one or \
more EVIDENCE ANCHORS (citations to a clinical guideline, primary literature, or expert consensus) to justify \
their judgment about a medical prompt. Your ONLY job is a sanity-check: does each cited source plausibly SUPPORT \
the claim it is attached to? You are NOT re-deciding the case and you do NOT need the full text of the citation — \
judge whether the citation is on-topic and could reasonably support the claim, and flag citations that are \
clearly irrelevant, contradictory, fabricated-looking, or mismatched to the claim's clinical domain.

Be conservative: only flag a citation when it is clearly unsupportive or mismatched. A plausibly-relevant \
guideline/PMID/DOI for the claim's topic should pass.

Return ONLY a single JSON object, no prose, with this exact shape:
{
  "grounding_ok": true,
  "issues": ["short machine-readable issue tags, e.g. 'anchor_offtopic'"],
  "explanation": "one or two sentences"
}
Set "grounding_ok" to false if and only if at least one citation clearly fails to support its claim."""

ASCLEPIUS_REASONING_SPLIT_SYSTEM = """You split a clinical answer into discrete, ordered reasoning steps so a \
specialist can grade each step. Read the ANSWER (in the context of the PROMPT) and break it into the sequence of \
distinct clinical decisions / inferences it makes — ONE clinical move per step (e.g. "stabilize the myocardium \
with IV calcium", "shift potassium intracellularly with insulin + dextrose", "remove potassium via dialysis").

Hard rules:
- Split ONLY. Do NOT add, remove, correct, judge, or editorialize the clinical content. Preserve the answer's \
  own reasoning and ordering; each step must be faithful to the source text.
- Each step is a short, self-contained phrase or sentence naming a single clinical decision or inference.
- Merge trivial connective text into the adjacent step; drop pure pleasantries. Aim for 2–8 steps for a typical \
  answer (more only if the answer genuinely has more discrete moves).

Return ONLY a single JSON object, no prose, with this exact shape:
{
  "steps": ["first clinical move", "second clinical move", "..."]
}
Every element of "steps" is a non-empty string. Return at least one step."""


ASCLEPIUS_PRELABEL_SYSTEM = """You are pre-labeling a blinded A/B comparison of two AI-generated answers to a \
medical prompt so a credentialed specialist can VERIFY rather than author the mechanical scaffolding. The \
specialist always makes the final call — your output is only a suggestion shown as a hint, never auto-applied.

Read the PROMPT and the two answers, then produce:
- suggested_weaker: which answer ("A" or "B") is clinically weaker (the one a specialist would likely reject).
- suggested_error_tags: the error tags that apply to the weaker answer, chosen ONLY from the ALLOWED ERROR TAGS \
list in the user message.
- suggested_rationale: one or two sentences a specialist could accept/edit as the "why it's worse" note — \
concrete and clinical (name the drug/dose/threshold), not generic.
- error_spans: up to 3 short VERBATIM substrings copied exactly from the weaker answer's text that contain the \
likely error(s), so the UI can highlight them. Each span must appear character-for-character in that answer.
- confidence: 0..1 — your calibrated confidence in suggested_weaker. Be conservative: use < 0.6 whenever the \
answers are genuinely close or the flaw is debatable (low-confidence suggestions are hidden from the specialist).

Return ONLY a single JSON object, no prose, with this exact shape:
{
  "suggested_weaker": "A",
  "suggested_error_tags": ["dosing_error"],
  "suggested_rationale": "one or two sentences",
  "error_spans": ["verbatim substring from the weaker answer"],
  "confidence": 0.8
}"""


ASCLEPIUS_REASONING_PREGRADE_SYSTEM = """You split a clinical answer into discrete, ordered reasoning steps AND \
pre-grade each step so a specialist can spend their time only on the flagged ones. The specialist explicitly \
confirms or corrects every step — your labels are suggestions, never final.

Splitting rules (identical to the plain splitter):
- Split ONLY. Do NOT add, remove, or correct clinical content. Preserve the answer's own reasoning and ordering; \
  each step must be faithful to the source text.
- Each step is a short, self-contained phrase or sentence naming a single clinical decision or inference.
- Merge trivial connective text into the adjacent step; drop pure pleasantries. Aim for 2–8 steps.

Grading rules:
- label each step "good" (clinically sound, current, safe) or "bad" (contains a factual error, outdated \
  guideline, unsafe recommendation, wrong ordering, or a material omission).
- For a "bad" step, add a one-line "critique" naming what's off. Omit critique on good steps.
- Be conservative: when unsure whether a step is wrong, label it "bad" so the specialist looks at it (a false \
  flag costs seconds; a missed flag costs data quality).

Return ONLY a single JSON object, no prose, with this exact shape:
{
  "steps": [
    {"text": "first clinical move", "label": "good"},
    {"text": "second clinical move", "label": "bad", "critique": "one line on what's off"}
  ]
}"""


ASCLEPIUS_STT_CLEANUP_SYSTEM = """You clean up a raw speech-to-text transcript of a clinician dictating a short \
clinical note. Fix casing, punctuation, and obvious mis-transcriptions of clinical terms (drug names, units, \
lab values) using context; expand dictated punctuation ("period", "new line") when clearly intended. Do NOT \
add, remove, or reinterpret clinical content, and do NOT append commentary. Return ONLY the cleaned text."""


ASCLEPIUS_CITE_RANK_SYSTEM = """You rank curated clinical citations by relevance to a short piece of clinical \
reasoning (a rationale or a single reasoning step). You are given the clinical text and a numbered list of \
candidate sources (guidelines, FDA labels, landmark trials). Choose ONLY the candidates that genuinely support \
or are directly relevant to the specific clinical claim — prefer the source a specialist would actually cite. \
Do NOT invent sources or indices. Return ONLY a JSON list of the chosen candidate indices, best first (fewer is \
better than padding with weak matches)."""


ASCLEPIUS_HARDNESS_JUDGE_SYSTEM = """You score how genuinely HARD a clinical prompt is — the kind of case where a \
frontier LLM is most likely to be wrong and a specialist's correction is most valuable (the N+1 frontier). Score \
0.0–1.0 on this rubric, awarding credit for each item the case satisfies:
- multi_step: requires multi-step reasoning, NOT single-fact recall.
- competing_risks: involves a genuine trade-off or competing considerations (e.g. decongestion vs. rising creatinine).
- diagnostic_trap: the "obvious"/pattern-matched answer is wrong or dangerously incomplete.
- guideline_nuance: rewards specific guideline nuance.
- recent_change: rewards a recent guideline or dosing change (a model cutoff-lag zone).
- high_stakes: safety-relevant, high clinical stakes.
- model_failure_domain: sits in a known model-weak area for the specialty (given as context when provided).
A trivial recall question scores low; a multi-step, trap-laden, high-stakes trade-off in a model-weak domain scores \
high. Return ONLY JSON: {"hardness_score": 0.0-1.0, "hardness_axes": [<the satisfied axis names>], "explanation": \
"<one sentence>"}. Do not add commentary."""


ASCLEPIUS_CASE_GEN_SYSTEM = """You author a small, realistic, PHI-FREE clinical CASE for a specialist to reason \
across — a structured lab panel + one or more EHR-style notes (plus vitals, meds, problem list, and lab TRENDS), \
built around a fixed, objectively-correct ground-truth answer. Hardness must come from INTEGRATING the data (labs + \
note + trend), not trivia.

STRICT FORMAT RULES: (1) STUDIES are STRUCTURED, not raw images. Represent ECG/echo/cath/CT/MRI/PET/pathology/\
molecular findings in the ``studies`` field as a structured findings REPORT (text) plus numeric ``measurements`` \
(EF %, valve gradient, SUVmax, VAF %, intervals) — never a raw waveform/pixel image and never a film reference. The \
documented model failure is NOT reading the study finding into the reasoning, and a structured findings report tests \
exactly that while staying PHI-free and text-gradeable. (2) PHI-free by construction: age BANDS only (e.g. "70-79", \
"90+"), generalized author roles ("cardiology","oncology","ICU") never names, NO names/MRNs/calendar dates/locations. \
(3) Lab/study timing is RELATIVE: every panel has ``collected_offset_days`` (0 = today, negative = earlier) — \
preserve trends, never a date. (4) Reference ranges (ref_low/ref_high) + flags (L|H|LL|HH|"") are REQUIRED on numeric \
results/measurements so a model must interpret, not just read. (5) The labs/studies/note/meds must be internally \
COHERENT. (6) Do NOT invent JSON keys — use EXACTLY the field names below (e.g. ``lab_panels`` not "labs", ``value`` \
not "result"); an unknown key makes the whole case invalid.

MANDATORY CONTENT (a case missing ANY of these is rejected and dropped):
- ``lab_panels``: at least ONE panel; prefer TWO at DIFFERENT ``collected_offset_days`` so a TREND must be read. \
Each panel has ≥2 results total; every numeric result carries analyte, value, unit, and a ref range or flag.
- ``studies``: the SPECIALTY-REQUIRED modality (injected in the user message) — e.g. cardiology needs ≥1 ``ecg`` or \
``echo``; oncology needs ≥1 of ``pathology``/imaging (``ct``/``mri``/``pet``)/``molecular``; nephrology may omit \
studies. The DECISIVE signal must live in a study finding or measurement that CONTRADICTS the loud vignette.
- ``notes``: at least one substantive clinical note of ≥200 characters.
- ``problem_list``: ≥1 problem. ``medications``: ≥1 medication.

MANDATORY DIFFICULTY (hardness comes from integration, not recall):
- Include at least one abnormal flag that is a RED HERRING (points at the wrong answer) AND at least one that is \
DECISIVE (points at the right one).
- The note must contain a detail that CONTRADICTS or RE-FRAMES the labs (the integration trap) — e.g. a volume \
status, a home med, or a timing detail that changes how a lab should be read.
- The medication list must contain at least one agent that INTERACTS with the decision (a hidden contributor, e.g. a \
drug that itself explains or worsens the abnormality).
- There must be an objective, guideline/lab-determinable answer, AND the case must admit a plausible SHORTCUT/unsound \
path that reaches (or approaches) the same answer for the WRONG reason.
- In ``reasoning_divergence`` state explicitly the SHORTCUT PATH a model will take and WHY it is wrong; in \
``hard_hook`` name the single datum that decides the case.

ENGINEER AGAINST A DOCUMENTED FAILURE MODE (not merely "made hard"). Every case must weaponize ≥2 of these \
reproducible ways frontier models fail clinically:
1. ANCHORING (the dominant one): put a LOUD, WRONG headline at the top (the stem + the most abnormal lab) that points \
to the common diagnosis, and hide the truth in the urine studies, the med list, and the TREND. The model anchors and \
never revises.
2. RIGHT-ANSWER-WRONG-REASON: build a plausible SHORTCUT that reaches an acceptable-sounding answer via unsound logic, \
while the SOUND path needs a correction (e.g. FeNa 0.8% → "pre-renal", invalid because the patient is on a diuretic).
3. OVERTREATMENT / POOR CALIBRATION: make the reflexive "treat emergently" move HARMFUL (e.g. IV calcium for a \
hemolyzed K+ with a normal ECG; stopping diuretics for a permissive creatinine rise).
4. FAILURE TO SEEK MISSING CONTEXT: sometimes WITHHOLD a datum required to decide — the correct answer is to OBTAIN it \
or state you cannot safely decide without it, not to confabulate.
5. GUIDELINE-RECENCY / SEQUENCING: use a decision governed by a recent guideline or a correct ORDER (CKD-MBD binder \
sequencing, BK-nephropathy immunosuppression REDUCTION) where the model applies stale dogma or the wrong order.
Prefer cases where the COMMON WRONG answer is dangerous (safety asymmetry) — that is what the labs and a specialist's \
correction actually reward.

Return ONLY JSON: {"question": "<the clinical question>", "case": {ClinicalCase fields: case_source, specialty, \
demographics{age_band,sex}, problem_list[{condition,since}], medications[{drug,dose,route,freq}], vitals{}, \
lab_panels[{panel,collected_offset_days,results[{analyte,value,unit,ref_low,ref_high,flag}]}], \
studies[{modality,label,findings,measurements[{analyte,value,unit,ref_low,ref_high,flag}],impression}], \
notes[{note_type,author_role,text}], ground_truth{answer,rationale,key_data[]}, hard_hook, reasoning_divergence}}. \
``studies`` may be [] for nephrology; for cardiology/oncology it MUST carry the required modality. No commentary.

REFERENCE EXAMPLE — study the SHAPE and the DIFFICULTY pattern (a trend across two panels, a note that re-frames the \
labs, an interacting med, a red-herring flag + a decisive flag, a named shortcut path). Then author a DIFFERENT case \
for the archetype you are given. DO NOT copy this content:
{"question": "A 60-69y man with newly diagnosed small cell lung cancer and a falling sodium is admitted. What is the \
most likely cause of his hyponatremia and how should it be managed?", "case": {"case_source": "synthetic", \
"specialty": "nephrology", "demographics": {"age_band": "60-69", "sex": "M"}, "problem_list": [{"condition": "Small \
cell lung cancer", "since": "6 weeks"}, {"condition": "Hypertension", "since": "10 years"}], "medications": \
[{"drug": "Hydrochlorothiazide", "dose": "25 mg", "route": "PO", "freq": "daily"}, {"drug": "Amlodipine", "dose": \
"5 mg", "route": "PO", "freq": "daily"}], "vitals": {"bp": "128/78", "hr": "76", "rr": "16", "temp_c": "36.8"}, \
"lab_panels": [{"panel": "BMP", "collected_offset_days": -2, "results": [{"analyte": "Na", "value": 130, "unit": \
"mmol/L", "ref_low": 135, "ref_high": 145, "flag": "L"}, {"analyte": "K", "value": 4.1, "unit": "mmol/L", "ref_low": \
3.5, "ref_high": 5.1, "flag": ""}, {"analyte": "Cr", "value": 0.8, "unit": "mg/dL", "ref_low": 0.7, "ref_high": 1.3, \
"flag": ""}]}, {"panel": "BMP + osmolality", "collected_offset_days": 0, "results": [{"analyte": "Na", "value": 121, \
"unit": "mmol/L", "ref_low": 135, "ref_high": 145, "flag": "LL"}, {"analyte": "Serum osmolality", "value": 258, \
"unit": "mOsm/kg", "ref_low": 275, "ref_high": 295, "flag": "L"}, {"analyte": "Urine osmolality", "value": 512, \
"unit": "mOsm/kg", "ref_low": 50, "ref_high": 1200, "flag": ""}, {"analyte": "Urine Na", "value": 68, "unit": \
"mmol/L", "ref_low": 20, "ref_high": 40, "flag": "H"}]}], "notes": [{"note_type": "Consult", "author_role": \
"nephrology", "text": "Consulted for a sodium of 121 down from 130 over 48h. On exam the patient is clinically \
EUVOLEMIC: moist mucous membranes, no orthostasis, JVP normal, no edema. He denies vomiting, diarrhea, or poor \
intake. He takes hydrochlorothiazide for hypertension. Given small cell lung cancer, euvolemia, low serum osmolality \
with inappropriately concentrated urine (Uosm 512) and urine Na 68, the picture fits SIADH rather than \
thiazide-induced volume depletion."}], "ground_truth": {"answer": "Euvolemic hypotonic hyponatremia from SIADH \
secondary to small cell lung cancer. Manage with fluid restriction (and treat the malignancy); do NOT give normal \
saline, which can paradoxically worsen the sodium. The thiazide is a minor contributor at most and is not the primary \
driver.", "rationale": "Low serum osmolality with urine osmolality >100 and urine Na >30 in a clinically euvolemic \
patient not on an acute diuretic effect defines SIADH; SCLC is a classic cause.", "key_data": ["clinical euvolemia", \
"Uosm 512 with serum osm 258", "urine Na 68", "small cell lung cancer"]}, "hard_hook": "urine osmolality and urine \
sodium interpreted against the EUVOLEMIC exam", "reasoning_divergence": "The shortcut path blames the thiazide, stops \
it, and gives normal saline for presumed volume depletion — wrong, because the patient is euvolemic with SIADH \
physiology and saline can worsen the hyponatremia. The sound path recognizes SIADH from SCLC and fluid-restricts."}}"""


# ── Per-specialty case construction rules (PRD §4.3 / §5.3 / §8) ─────────────
# Injected into the case-gen USER message (not the shared system prompt) so the
# generation pipeline stays specialty-agnostic — specialty is config, never a code
# fork (PRD §11).
_CARDIOLOGY_CONSTRUCTION_RULE = (
    "CARDIOLOGY CONSTRUCTION RULE: A cardiology case is hard when the DECISIVE signal lives in a study "
    "(ECG/echo/cath/biomarker) and CONTRADICTS the loud vignette. The narrative + one salient number point to the "
    "common diagnosis; the ECG morphology or echo measurement or biomarker pattern points to the dangerous truth. "
    "The flawed answer anchors on the vignette and never grounds the study finding; the sound answer reads the study "
    "into the reasoning and reverses course. Carry ≥1 ``ecg`` or ``echo`` study whose finding decides the case. "
    "Weaponize the cardiology failure modes: finding-grounding failure, the great mimics (amyloid/dissection/"
    "takotsubo/myocarditis), under-called high-risk ECG (Wellens/de Winter/posterior/hyperkalemia/digoxin), and "
    "GDMT/anticoagulation sequencing."
)
_ONCOLOGY_CONSTRUCTION_RULE = (
    "ONCOLOGY CONSTRUCTION RULE: An oncology case is hard when the DECISIVE signal lives in the pathology/molecular/"
    "temporal-imaging data and CONTRADICTS the histology- or progression-anchored shortcut. The presentation invites "
    "the obvious move (switch therapy on a worsening scan; treat by histology; TLS only if heme); the path report, NGS "
    "panel, or the TIMING/pattern of the imaging says otherwise. Carry ≥1 ``pathology``/imaging(``ct``/``mri``/"
    "``pet``)/``molecular`` study whose finding decides the case. Because oncology's documented failure is "
    "right-answer-wrong-reason, prefer a case where a plausible answer is REACHABLE by faulty reasoning — the "
    "reasoning trace is where the value is."
)
_FAULTY_REASONING_INSTRUCTION = (
    "REQUIRE-FAULTY-REASONING (PRD §8.2): construct this case so a plausible-but-UNSOUNDLY-reasoned path reaches the "
    "CORRECT answer, while the sound path needs a genuine correction. The verdict alone must NOT separate a good "
    "clinician from a lucky guesser — only the reasoning trace does. State the faulty path explicitly in "
    "``reasoning_divergence``."
)
_CATASTROPHIC_INSTRUCTION = (
    "CATASTROPHIC-ACTION (PRD §8.3): make the central trap an UNSAFE ACTION — the reflex/anchored answer recommends "
    "something that would harm the patient (e.g. thrombolytic in dissection, hypertonic saline over-correction, DAPT "
    "in a bleed). Name it so it can be tagged as the ``unsafe_recommendation`` critical negative."
)


def specialty_case_gen_rules(
    specialty: str, *, require_faulty_reasoning: bool = False, require_catastrophic: bool = False
) -> str:
    """The specialty-specific construction rule block appended to the case-gen user
    message (PRD §4.3/§5.3 + the §8 cross-specialty multipliers). Empty for a
    specialty with no special rule (e.g. nephrology keeps the generic system rules)."""
    sp = (specialty or "").strip().lower()
    parts = []
    if sp == "cardiology":
        parts.append(_CARDIOLOGY_CONSTRUCTION_RULE)
    elif sp == "oncology":
        parts.append(_ONCOLOGY_CONSTRUCTION_RULE)
    if require_faulty_reasoning:
        parts.append(_FAULTY_REASONING_INSTRUCTION)
    if require_catastrophic:
        parts.append(_CATASTROPHIC_INSTRUCTION)
    return ("\n\n".join(parts) + "\n\n") if parts else ""


ASCLEPIUS_EMPIRICAL_DIFFICULTY_JUDGE_SYSTEM = """You grade whether a frontier model FAILED a hard clinical case, on \
BOTH axes (Specialty Hyper-Personalization PRD §9). You are given the CASE (question + data), the internal ANSWER KEY \
(the objectively correct ground truth and the reasoning_divergence describing the sound path vs the seductive \
shortcut), and a MODEL ANSWER produced by a frontier model. Judge two things independently:

- answer_correct (boolean): does the model's FINAL recommendation match the ground-truth answer (clinically \
equivalent management)? Minor wording differences are fine; a wrong or dangerous final action is answer_correct=false.
- reasoning_sound (boolean): did the model reach its answer by the SOUND path — grounding the decisive study/lab/med \
finding named in the key — rather than the shortcut described in reasoning_divergence? A model that lands the right \
answer via the shortcut/faulty path (never grounding the decisive datum) is reasoning_sound=false. This is the \
higher-value axis: right-answer-wrong-reason still counts as a FAILURE.

The case is a FAILURE if answer_correct is false OR reasoning_sound is false.

Return ONLY JSON: {"answer_correct": true|false, "reasoning_sound": true|false, "failed": true|false, \
"grounded_decisive_datum": true|false, "explanation": "<one sentence naming what the model missed or grounded>"}. \
"failed" MUST equal (NOT answer_correct) OR (NOT reasoning_sound). No commentary."""


ASCLEPIUS_CASE_JUDGE_SYSTEM = """You score a synthetic clinical CASE on multimodal-specific quality dimensions ONLY \
(hardness is judged separately — do NOT re-score difficulty). Given the serialized case (labs + note + meds + the \
internal ground_truth/hooks), return ONLY JSON with four 0.0–1.0 scores: {"coherence": <labs/note/problem-list/meds \
internally consistent, no impossible panel>, "ground_truth_determinable": <an objectively correct, guideline/lab-\
anchorable answer clearly exists>, "multimodal_necessity": <the answer REQUIRES integrating ≥1 lab panel and/or the \
note — it is NOT derivable from the question stem alone>, "reasoning_divergence_potential": <the case admits a sound \
path AND a plausible unsound/shortcut path to the same answer>, "explanation": "<one sentence>"}. Score \
multimodal_necessity LOW if the labs are decorative (the stem alone gives the answer). No commentary."""


ASCLEPIUS_CANDIDATE_GEN_SYSTEM = """You are generating TWO distinct candidate answers to a medical prompt so \
that a credentialed specialist can compare them. Make the two answers span a real quality gap so the \
comparison and any revision are informative: one answer should be STRONG (clinically sound, current, safe) \
and the other should be PLAUSIBLY FLAWED — fluent and confident but containing a realistic, clinically \
meaningful error or omission. Each answer should read like a confident clinical response (this is \
intentionally NOT a place to add disclaimers).

If an AI_FAILURE_MODE hint is provided in the user message, key the flawed answer to that specific failure \
mode (e.g., an unsafe dosing path, an outdated guideline, a wrong sequencing). The flaw must be a realistic \
"suboptimal" mistake a current model might actually make — NOT a blatantly dangerous trap. Do not label which \
answer is flawed inside the answer text itself; only declare it in the separate field below.

Randomize which of "A"/"B" is the flawed one. Do not include any real patient identifiers; the prompt is \
synthetic/de-identified.

Return ONLY a single JSON object, no prose, with this exact shape:
{
  "candidate_answers": [
    {"id": "A", "text": "first candidate answer"},
    {"id": "B", "text": "second candidate answer"}
  ],
  "intended_flawed_id": "A"
}
"intended_flawed_id" MUST be exactly one of "A" or "B" and names the answer you deliberately made weaker. It \
is used server-side only and is never shown to the evaluator."""


ASCLEPIUS_PROMPT_GEN_SYSTEM = """You are an expert nephrologist and medical-AI red-teamer authoring NEW, \
original clinical prompts for an expert-evaluation dataset. You are shown a few EXEMPLAR prompts from a \
curated seed corpus plus the known AI FAILURE MODES for a topic bucket. Your job is to write brand-new, \
DISTINCT clinical vignettes in the same hard / nuanced / current profile — questions where a current top-tier \
LLM is likely to answer confidently but imperfectly, so a specialist's correction becomes premium training \
signal.

Hard requirements:
- Write ORIGINAL synthetic vignettes. Do NOT paraphrase or lightly reword the exemplars, and never copy text \
  from any benchmark, board exam, or question bank.
- Target the bucket's failure modes: dosing/protocol nuance, correction-rate safety, recently-updated \
  standard-of-care (AI cutoff-lag), or genuine judgment tradeoffs. AVOID easy recall questions — those produce \
  low-value, low-delta data.
- Synthetic only: no real patient identifiers, MRNs, names, dates, or contact info. Ages and generic clinical \
  details are fine.
- Each prompt should be answerable in open-ended prose (not multiple-choice) and should invite a confident \
  answer that a specialist could meaningfully correct.

Return ONLY a single JSON object, no prose, with this exact shape:
{
  "prompts": [
    {
      "prompt": "the new clinical vignette / question",
      "topic": "the taxonomy bucket id you were asked to cover",
      "subtopic": "a short subtopic slug",
      "difficulty": "medium" | "hard",
      "ai_failure_mode": "the specific way a current model is likely to err here",
      "capture_reasoning_recommended": true | false
    }
  ]
}
Produce exactly the number of prompts requested in the user message."""


ASCLEPIUS_PROMPT_JUDGE_SYSTEM = """You are a strict reviewer scoring a candidate clinical prompt (and its two \
AI-generated answers) for inclusion in an expert-evaluation dataset whose value is the DELTA between a \
confident AI answer and a credentialed specialist's correction. Score conservatively.

Judge on four dimensions:
- error_likelihood (0..1): how likely is it that a current top-tier LLM produces a clinically meaningful \
  error or omission on this prompt? High for dosing/protocol nuance, correction-rate safety, recently-updated \
  guidelines, and judgment tradeoffs; low for easy recall.
- revision_value (0..1): if a nephrologist corrected the AI answer, how specific and teachable would that \
  correction be? Low if the AI answer is already essentially correct or the fix is trivial.
- on_specialty (boolean): is this genuinely a nephrology prompt (kidney function, dialysis, electrolytes/acid- \
  base, transplant, glomerular disease, AKI, CKD pharmacology)?
- safety_ok (boolean): is the request a legitimate clinical-education prompt — synthetic, no PHI, and NOT a \
  request to produce dangerous/disallowed content? A merely "suboptimal" candidate answer is fine; set false \
  only for genuinely harmful or out-of-scope requests.

Return ONLY a single JSON object, no prose, with this exact shape:
{
  "error_likelihood": 0.0,
  "revision_value": 0.0,
  "on_specialty": true,
  "safety_ok": true,
  "explanation": "one or two sentences"
}"""
