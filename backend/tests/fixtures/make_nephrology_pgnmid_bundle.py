"""Generator for ``nephrology_pgnmid_bundle.json`` — the masked PGNMID partner
bundle used as the ground-truth fixture for the Buyer Response PRD tests (§12).

Run ``python tests/fixtures/make_nephrology_pgnmid_bundle.py`` to regenerate the
JSON. It is committed alongside this script so tests run against the real artifact,
not a hand-made mock that happens to match our assumptions. The bundle is declared
synthetic and carries no real PHI — the identity fields on the Patient exist only to
exercise the de-identification guard.

The clinical content is a masked PGNMID / MGRS case: routine frozen IF is
C3-dominant and near-negative for IgG; EM shows numerous deposits; pronase-digested
paraffin IF unmasks IgG3-kappa restriction. The trap is anchoring on resolved MSSA
infection plus low C3 and calling it post-infectious GN.
"""
from __future__ import annotations

import base64
import io
import json
import os

FHIR_BASE = "https://archangel.health/fhir/StructureDefinition"


def _png(color: tuple) -> str:
    """A tiny but genuine PNG (2x2) so assets.process_upload can decode/normalize it.
    Different colors → different sha256, so the five studies get distinct assets."""
    from PIL import Image

    im = Image.new("RGB", (2, 2), color)
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _b64_text(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _obs(oid, code, value, unit, low, hi, when, flag=None, loinc=None):
    coding = [{"display": code}]
    if loinc:
        coding.insert(0, {"system": "http://loinc.org", "code": loinc, "display": code})
    res = {
        "resourceType": "Observation",
        "id": oid,
        "status": "final",
        "category": [{"coding": [{"code": "laboratory"}]}],
        "code": {"coding": coding, "text": code},
        "effectiveDateTime": when,
        "valueQuantity": {"value": value, "unit": unit},
        "referenceRange": [{"low": {"value": low}, "high": {"value": hi}}],
    }
    if flag:
        res["interpretation"] = [{"coding": [{"code": flag}]}]
    return res


def build() -> dict:
    entries: list = []

    def add(res):
        entries.append({"resource": res})

    # ── Patient — full identity set + a NEVER-TRUSTED synthetic flag (§2 A5) ──
    add({
        "resourceType": "Patient",
        "id": "patient-1",
        "name": [{"family": "Price", "given": ["Eleanor", "V"]}],
        "telecom": [{"system": "phone", "value": "(555) 010-8821"}],
        "gender": "female",
        "birthDate": "1961-11-08",
        "address": [{"city": "Sacramento", "state": "CA", "postalCode": "958XX"}],
        "identifier": [{"system": "urn:mrn", "value": "SYN-MRN-8841902"}],
        "extension": [{"url": f"{FHIR_BASE}/synthetic-patient", "valueBoolean": True}],
    })

    add({"resourceType": "Encounter", "id": "enc-1", "status": "finished",
         "class": {"code": "IMP"}})

    # ── 45 laboratory Observations across five encounters (longitudinal) ──
    dates = ["2025-01-06", "2025-01-27", "2025-02-17", "2025-03-03", "2025-03-17"]
    panels = [
        ("Creatinine", "mg/dL", 0.6, 1.2, "H", "2160-0"),
        ("Blood urea nitrogen", "mg/dL", 7, 20, "H", "3094-0"),
        ("eGFR", "mL/min/1.73m2", 90, 120, "L", "48642-3"),
        ("Albumin", "g/dL", 3.5, 5.0, "L", "1751-7"),
        ("C3 complement", "mg/dL", 90, 180, "L", "4485-1"),
        ("C4 complement", "mg/dL", 10, 40, None, "4498-4"),
        ("Hemoglobin", "g/dL", 12, 16, "L", "718-7"),
        ("Urine protein/creatinine ratio", "mg/mg", 0.0, 0.2, "H", "34366-5"),
        ("Serum free kappa", "mg/L", 3.3, 19.4, "H", "36916-5"),
    ]
    # curated values that trend over time (renal function worsening, C3 low)
    trend = {
        "Creatinine": [1.1, 1.4, 1.8, 2.3, 2.6],
        "Blood urea nitrogen": [18, 24, 31, 38, 44],
        "eGFR": [72, 58, 44, 33, 28],
        "Albumin": [3.6, 3.2, 2.9, 2.7, 2.6],
        "C3 complement": [78, 74, 70, 69, 71],
        "C4 complement": [22, 21, 20, 19, 20],
        "Hemoglobin": [12.4, 11.8, 11.2, 10.9, 10.6],
        "Urine protein/creatinine ratio": [1.8, 2.6, 3.4, 4.1, 4.6],
        "Serum free kappa": [24, 31, 39, 47, 52],
    }
    oid = 0
    for di, when in enumerate(dates):
        for (name, unit, low, hi, flag, loinc) in panels:
            oid += 1
            val = trend[name][di]
            f = flag if (di >= 1) else None
            add(_obs(f"obs-{oid}", name, val, unit, low, hi, f"{when}T09:00:00Z",
                     flag=f, loinc=loinc))

    # ── 6 MedicationStatements ──
    for mid, (drug, dose, route, freq) in enumerate([
        ("Lisinopril", "10 mg", "oral", "daily"),
        ("Furosemide", "40 mg", "oral", "twice daily"),
        ("Atorvastatin", "20 mg", "oral", "nightly"),
        ("Cholecalciferol", "2000 units", "oral", "daily"),
        ("Pantoprazole", "40 mg", "oral", "daily"),
        ("Cefazolin", "2 g", "intravenous", "completed course"),
    ], start=1):
        add({
            "resourceType": "MedicationStatement", "id": f"med-{mid}",
            "status": "active",
            "medicationCodeableConcept": {"text": drug},
            "dosage": [{
                "route": {"text": route},
                "timing": {"code": {"text": freq}},
                "doseAndRate": [{"doseQuantity": {"value": float(dose.split()[0]),
                                                  "unit": dose.split()[1]}}],
            }],
        })

    # ── 5 Conditions (problem list) ──
    for cid, cond in enumerate([
        "Acute kidney injury", "Proteinuria", "Microscopic hematuria",
        "Resolved methicillin-sensitive Staphylococcus aureus bacteremia",
        "Anemia of chronic disease",
    ], start=1):
        add({"resourceType": "Condition", "id": f"cond-{cid}",
             "code": {"text": cond}})

    # ── 6 clinical notes (DocumentReference, base64 text attachments) ──
    notes = [
        ("Nephrology H&P",
         "Adult presenting with subacute rise in creatinine, new proteinuria and "
         "microscopic hematuria. Recent hospitalization for a bloodstream infection, "
         "since treated and resolved. Blood pressure mildly elevated. No rash, no "
         "arthralgia. The differential prioritizes a proliferative glomerulonephritis "
         "and the interplay of a low complement level with a recently resolved "
         "infection is noted but deliberately not yet adjudicated."),
        ("Nephrology progress note",
         "Renal function continues to decline across serial measurements with rising "
         "urine protein to creatinine ratio. Complement remains persistently low. The "
         "team elects to proceed with a percutaneous renal biopsy for tissue diagnosis "
         "given the trajectory and the diagnostic uncertainty between an infection "
         "related process and a monoclonal process."),
        ("Renal pathology gross description",
         "A percutaneous core of renal cortex and medulla is received for light "
         "microscopy, immunofluorescence and electron microscopy. Adequate glomeruli "
         "are present for evaluation. Ancillary staining including a paraffin "
         "immunofluorescence protocol with protease antigen retrieval is arranged."),
        ("Hematology consult",
         "Consulted for a persistently elevated serum free light chain with an "
         "abnormal ratio in the setting of kidney disease. A bone marrow evaluation "
         "and peripheral flow are recommended to characterize any underlying clonal "
         "lymphoproliferative or plasma cell population contributing to renal injury."),
        ("Infectious disease note",
         "The prior bloodstream infection was treated to completion with clinical "
         "resolution and negative surveillance cultures. There is no evidence of "
         "ongoing endovascular infection. The persistent low complement and worsening "
         "renal picture are unlikely to be explained by the resolved infection alone."),
        ("Nursing note",
         "Patient tolerated the biopsy procedure without immediate complication. Vital "
         "signs stable throughout the observation window. Urine output monitored and "
         "documented. No gross hematuria after the procedure. Education provided on "
         "post procedure activity restrictions and warning signs to report."),
    ]
    for nid, (ntype, text) in enumerate(notes, start=1):
        add({
            "resourceType": "DocumentReference", "id": f"doc-{nid}",
            "type": {"text": ntype},
            "content": [{"attachment": {"contentType": "text/plain",
                                        "data": _b64_text(text)}}],
        })

    # ── DiagnosticReport (flow cytometry) — answer-bearing conclusion (§2 A6) ──
    add({
        "resourceType": "DiagnosticReport", "id": "report-flow",
        "status": "final",
        "type": {"text": "Flow cytometry, peripheral blood"},
        "code": {"text": "Flow cytometry, peripheral blood"},
        "effectiveDateTime": "2025-03-10T09:00:00Z",
        "conclusion": (
            "Flow cytometry of peripheral blood was performed. A small "
            "kappa-restricted CD20-positive B-cell population is present. This finding "
            "is below criteria for overt lymphoma but may be clinically significant in "
            "the setting of renal monoclonal immunoglobulin deposition."
        ),
    })

    # ── 5 Media → Study.asset. Neutral title, interpretive note (§2 A2, §3 B2) ──
    media = [
        ("media-urine", "Urine sediment, phase contrast 400x", (240, 240, 220),
         "Dysmorphic erythrocytes and an RBC cast."),
        ("media-lm", "Renal biopsy, light microscopy (PAS) 400x", (230, 200, 210),
         "Endocapillary hypercellularity, mesangial expansion, duplicated capillary contours."),
        ("media-ifc3", "Routine frozen IF, C3", (200, 230, 210),
         "Bright granular C3 with little to no IgG on routine frozen IF."),
        ("media-pronase", "Pronase paraffin IF, kappa and lambda", (210, 210, 240),
         "Strong kappa staining with absent lambda, revealing monotypic deposits."),
        ("media-em", "Electron microscopy", (220, 220, 200),
         "Numerous granular subendothelial and mesangial electron dense deposits."),
    ]
    for mid, title, color, note in media:
        add({
            "resourceType": "Media", "id": mid,
            "status": "completed",
            "content": {"contentType": "image/png", "title": title, "data": _png(color)},
            "note": [{"text": note}],
        })

    # ── Basic/eval-task — the partner's authored question (§2 A4) ──
    add({
        "resourceType": "Basic", "id": "eval-task",
        "code": {"text": "Evaluation task"},
        "extension": [
            {"url": f"{FHIR_BASE}/task-prompt",
             "valueString": (
                 "Given the longitudinal labs, notes and renal pathology, provide the "
                 "most likely unifying diagnosis and the single next test that confirms "
                 "it. State why the initial infection-related hypothesis is a trap.")},
            {"url": f"{FHIR_BASE}/difficulty", "valueString": "frontier-hard"},
            {"url": f"{FHIR_BASE}/required-modalities",
             # '+'-delimited technique tokens, matching how the real partner bundle
             # writes them (Audit PRD §P1); resolved via _MODALITY_SYNONYMS.
             "valueString": ("longitudinal labs + clinical notes + urine microscopy + "
                             "LM + routine IF + pronase IF + EM + hematology studies")},
        ],
    })

    # ── Basic/sealed-ground-truth — the adjudicated answer key (§3 B1) ──
    sealed = {
        "diagnosis": "Proliferative GN with monoclonal IgG deposits, MGRS spectrum",
        "why_initial_fails": (
            "Resolved MSSA bacteremia plus a depressed complement invites a "
            "post-infectious label, but the pathology reveals a monotypic process."),
        "decisive_evidence": [
            "pronase paraffin technique reveals monotypic light chain restriction",
            "electron microscopy shows abundant organized deposits discordant with routine stain",
            "serum free light chain ratio is abnormal and rising",
            "peripheral clone identified on hematology workup",
            "complement depression persists despite infection resolution",
        ],
        "management": "clone directed therapy, rituximab based, with nephrology comanagement",
        "critical_errors": [
            "committing to post-infectious GN without paraffin retrieval",
            "ignoring the discordance between routine stain and ultrastructure",
            "attributing the low complement solely to the resolved infection",
            "omitting a hematologic clonal workup",
            "stopping at the routine frozen panel",
        ],
        "reasoning_traps": [
            "low complement anchoring", "recent infection anchoring",
            "routine immunofluorescence false reassurance",
            "single timepoint interpretation", "premature closure",
        ],
    }
    add({
        "resourceType": "Basic", "id": "sealed-ground-truth",
        "code": {"text": "Internal adjudicated answer key — remove before model evaluation"},
        "extension": [{"url": f"{FHIR_BASE}/sealed-json",
                       "valueString": json.dumps(sealed)}],
    })

    # ── Provenance — 7 citations + agents + disclaimer (§2 A3) ──
    def entity(display, ident):
        return {"role": "source", "what": {"display": display,
                                           "identifier": {"value": ident}}}
    add({
        "resourceType": "Provenance",
        "id": "prov-1",
        "agent": [
            {"type": {"text": "author"}, "who": {"display": "referring nephrology"}},
            {"type": {"text": "adjudicator"}, "who": {"display": "renal pathology"}},
        ],
        "entity": [
            entity("KDIGO 2021 Glomerular Diseases Guideline", "10.1016/j.kint.2021.05.021"),
            entity("IKMG MGRS consensus report", "10.1038/s41581.019.0129.4"),
            entity("Masked monoclonal deposits and paraffin immunofluorescence", "10.1111/his.13478"),
            entity("Protease digestion immunofluorescence technique", "10.1093/ajcp/aqaa105"),
            entity("Monoclonal gammopathy of renal significance review", "10.1053/j.ackd.2020.06.004"),
            entity("Clinical agent benchmark for diagnostic reasoning", "arXiv:2311.12983"),
            entity("Multimodal medical agent evaluation", "arXiv:2407.19783"),
        ],
        "extension": [{"url": f"{FHIR_BASE}/disclaimer",
                       "valueString": "Synthetic, de-identified teaching case; not a real patient."}],
    })

    return {"resourceType": "Bundle", "type": "collection", "entry": entries}


def main() -> None:
    bundle = build()
    out = os.path.join(os.path.dirname(__file__), "nephrology_pgnmid_bundle.json")
    with open(out, "w") as f:
        json.dump(bundle, f, indent=2)
    counts: dict = {}
    for e in bundle["entry"]:
        rt = e["resource"]["resourceType"]
        counts[rt] = counts.get(rt, 0) + 1
    print("wrote", out)
    print("entries:", len(bundle["entry"]), counts)


if __name__ == "__main__":
    main()
