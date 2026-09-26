"""Regenerate entirely hand-authored synthetic fixtures; never reads source charts.

One-time generation needs reportlab and Pillow. Tests consume the committed
outputs and do not need a JVM, a network, an OCR engine, or a PDF renderer.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent
SOURCE_DAY = date(2024, 1, 15)


def worksheet(patient: int, visit: int, day: str) -> str:
    action = visit % 2 == 1
    current_dose = 20 / 2 ** (visit // 2)
    return (
        f"DOS: {day}\nVisit worksheet\nNephrology follow-up\n"
        f"Interval history: stable appetite and urine output. Visit {visit + 1}.\n"
        f"Labs: creatinine {2.1 + patient / 10:.1f} mg/dL, eGFR 28 mL/min/1.73m2, "
        f"potassium {5.6 if action else 4.7} mmol/L.\n"
        f"Current medications: Prinivil {current_dose:g} mg once daily, Norvasc 5 mg once daily.\n"
        "Assessment: CKD stage 4 (N18.4). Hypertension (I10).\nPlan: "
        + (f"Decrease Prinivil to {current_dose / 2:g} mg once daily. Check BMP in 7 days. " if action else
           "Continue Prinivil and Norvasc without changes. ")
        + "Avoid NSAIDs. Low potassium diet. Follow up in 28 days.\n"
    )


def case(patient: int) -> dict:
    count = 3 + (patient - 1) % 6
    notes, labs, encounters, orders, events = [], [], [], [], []
    for visit in range(count):
        offset = (visit - count + 1) * 100
        ref = f"enc-{visit:03d}"
        text = worksheet(patient, visit, "[visit day]")
        notes.append({"note_id": f"note-{visit}", "note_type": "Visit worksheet", "author_role": "nephrology",
                      "collected_offset_days": offset, "text": text})
        labs.append({"panel": "Renal", "collected_offset_days": offset - 1, "results": [
            {"analyte": "Creatinine", "loinc": "2160-0", "value": round(2.1 + patient / 10, 1), "unit": "mg/dL"},
            {"analyte": "eGFR", "loinc": "33914-3", "value": 28, "unit": "mL/min/1.73m2"},
            {"analyte": "Potassium", "loinc": "2823-3", "value": 5.6 if visit % 2 else 4.7, "unit": "mmol/L"},
            {"analyte": "UACR", "loinc": "9318-7", "value": 120, "unit": "mg/g"},
        ]})
        encounters.append({"encounter_ref": ref, "collected_offset_days": offset, "note_ids": [f"note-{visit}"]})
        orders.append({"kind": "follow_up", "text": "Follow up in 28 days", "encounter_ref": ref,
                       "collected_offset_days": offset, "due_offset_days": 28})
        if visit % 2:
            orders.append({"kind": "lab", "code": "24323-8", "code_system": "http://loinc.org", "text": "BMP",
                           "encounter_ref": ref, "collected_offset_days": offset, "due_offset_days": 7})
            events.append({"action": "decrease", "drug": "Prinivil", "rxnorm_ingredient": "29046", "dose": f"{20 / 2 ** (visit // 2 + 1):g} mg",
                           "freq": "once daily", "encounter_ref": ref, "collected_offset_days": offset})
    return {"case_id": f"synthetic-{patient:02d}", "case_source": "synthetic", "specialty": "nephrology",
            "demographics": {"age_band": "60-69", "sex": "F" if patient % 2 else "M"},
            "problem_list": [{"condition": "CKD stage 4 (N18.4)", "collected_offset_days": -900}],
            "medications": [{"drug": "Prinivil", "dose": "20 mg", "route": "oral", "freq": "once daily",
                             "status": "active", "collected_offset_days": -900, "start_offset_days": -900},
                            {"drug": "Norvasc", "dose": "5 mg", "route": "oral", "freq": "once daily",
                             "status": "active", "collected_offset_days": -900, "start_offset_days": -900}],
            "notes": notes, "lab_panels": labs, "encounters": encounters, "orders": orders,
            "medication_events": events,
            "allergies": [{"substance": "penicillin", "reaction": "rash", "collected_offset_days": -900}]}


def ccda(patient: int, count: int) -> bytes:
    sections = []
    sections.append('<component><section><code code="11450-4"/><entry><act><entryRelationship><observation>'
                    '<value code="N18.4" codeSystem="2.16.840.1.113883.6.90" displayName="CKD stage 4"/>'
                    '</observation></entryRelationship></act></entry></section></component>')
    for visit in range(count):
        day = SOURCE_DAY + timedelta(days=visit * 100)
        stamp = day.strftime('%Y%m%d')
        lab_stamp = (day - timedelta(days=1)).strftime('%Y%m%d')
        text = escape(worksheet(patient, visit, day.isoformat()))
        sections.append(f'<component><section><code code="51848-0"/><title>Visit worksheet</title>'
                        f'<text>{text}</text><entry><act><effectiveTime value="{stamp}"/></act></entry>'
                        '</section></component>')
        sections.append(f'<component><section><code code="46240-8"/><entry><encounter>'
                        f'<effectiveTime value="{stamp}"/><code displayName="Office visit"/>'
                        '</encounter></entry></section></component>')
        observations = ''.join(
            f'<component><observation><code code="{code}" codeSystem="2.16.840.1.113883.6.1" displayName="{name}"/>'
            f'<effectiveTime value="{lab_stamp}"/><value xsi:type="PQ" value="{value}" unit="{unit}"/>'
            '</observation></component>'
            for name, code, value, unit in [('Creatinine', '2160-0', round(2.1 + patient / 10, 1), 'mg/dL'),
                                          ('eGFR', '33914-3', 28, 'mL/min/1.73m2'),
                                          ('Potassium', '2823-3', 5.6 if visit % 2 else 4.7, 'mmol/L')])
        sections.append(f'<component><section><code code="30954-2"/><entry><organizer><code displayName="Renal"/>'
                        f'<effectiveTime value="{lab_stamp}"/>{observations}</organizer></entry></section></component>')
    for drug, dose in [('Prinivil', 20), ('Norvasc', 5)]:
        sections.append('<component><section><code code="10160-0"/><entry><substanceAdministration>'
                        '<statusCode code="active"/><effectiveTime><low value="20230101"/></effectiveTime>'
                        f'<doseQuantity value="{dose}" unit="mg"/><routeCode displayName="oral"/>'
                        '<consumable><manufacturedProduct><manufacturedMaterial>'
                        f'<code displayName="{drug}"/></manufacturedMaterial></manufacturedProduct></consumable>'
                        '</substanceAdministration></entry></section></component>')
    return ('<?xml version="1.0"?>\n<?xml-stylesheet type="text/xsl" href="not-fetched.xsl"?>\n'
            '<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f'<recordTarget><patientRole><id root="synthetic-test" extension="fixture-{patient}"/>'
            f'<patient><administrativeGenderCode code="{"F" if patient % 2 else "M"}"/>'
            '<birthTime value="19600101"/></patient></patientRole></recordTarget>'
            '<component><structuredBody>' + ''.join(sections) +
            '</structuredBody></component></ClinicalDocument>').encode()


def pdf(patient: int, count: int, scanned: bool = False) -> bytes:
    from reportlab.pdfgen import canvas
    from reportlab.lib.utils import ImageReader
    from PIL import Image, ImageDraw
    out = io.BytesIO()
    doc = canvas.Canvas(out, invariant=1, pagesize=(612, 792))
    for visit in range(count):
        day = SOURCE_DAY + timedelta(days=visit * 100)
        text = worksheet(patient, visit, day.isoformat())
        if scanned:
            img = Image.new('RGB', (1200, 1500), 'white')
            ImageDraw.Draw(img).multiline_text((40, 50), text, fill='black', spacing=12)
            doc.drawImage(ImageReader(img), 30, 70, width=550, height=690)
        else:
            cursor = doc.beginText(40, 745)
            cursor.setFont('Helvetica', 10)
            import textwrap
            for line in text.splitlines():
                for chunk in textwrap.wrap(line, 95) or ['']:
                    cursor.textLine(chunk)
            doc.drawText(cursor)
        doc.showPage()
    doc.save()
    return out.getvalue()


def labs_csv(patient: int, count: int) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator='\n')
    writer.writerow(['patient_key', 'panel', 'analyte', 'loinc', 'value', 'unit', 'collected_at'])
    for visit in range(count):
        day = (SOURCE_DAY + timedelta(days=visit * 100 - 1)).isoformat()
        for analyte, code, val, unit in [('Creatinine','2160-0',round(2.1 + patient / 10, 1),'mg/dL'), ('eGFR','33914-3',28,'mL/min/1.73m2'),
                                       ('Potassium','2823-3',5.6 if visit % 2 else 4.7,'mmol/L')]:
            writer.writerow([f'p{patient:02d}', 'Renal', analyte, code, val, unit, day])
    return out.getvalue().encode()


def archive(path: Path, entries: dict[str, bytes]) -> None:
    with ZipFile(path, 'w') as z:
        for name, data in sorted(entries.items()):
            item = ZipInfo(name, date_time=(2024, 1, 1, 0, 0, 0))
            item.compress_type = ZIP_DEFLATED
            item.external_attr = 0o644 << 16
            z.writestr(item, data)


def qualification_archive() -> None:
    entries={};mapping={}
    for i in range(1,49):
        if i<=24:
            raw=ccda(i,4).replace(b'<doseQuantity',b'<effectiveTime xsi:type="PIVL_TS"><period value="24" unit="h"/></effectiveTime><doseQuantity')
            files={f'p{i:02}__chart.xml':raw}
        else:
            files={f'p{i:02}__worksheets.pdf':pdf(i,4),f'p{i:02}__labs.csv':labs_csv(i,4)}
        entries.update(files);mapping.update({name:f'p{i:02}' for name in files})
    entries['manifest.json']=json.dumps({'specialty':'nephrology','deidentified':True,'note_type':'Visit worksheet','files':mapping}).encode()
    archive(ROOT / 'qualification_patients.zip', entries)


def generate() -> None:
    qualification_archive()
    (ROOT / 'cases').mkdir(exist_ok=True)
    entries, mapping, patients = {}, {}, []
    for patient in range(1, 13):
        data = case(patient)
        count = len(data['notes'])
        label = f'p{patient:02d}'
        (ROOT / 'cases' / f'{label}.json').write_text(json.dumps(data, indent=2) + '\n')
        files = {}
        if patient <= 4:
            files[f'{label}__chart.xml'] = ccda(patient, count)
            fmt = 'ccda'
        elif patient <= 10:
            files[f'{label}__worksheets.pdf'] = pdf(patient, count, scanned=patient >= 9)
            fmt = 'scanned_pdf' if patient >= 9 else 'text_pdf'
            # A parsed companion anchors patient identity when scans are unreadable;
            # the incomplete-upload gate must still hold every resulting case.
            files[f'{label}__labs.csv'] = labs_csv(patient, count)
        else:
            files[f'{label}__labs.csv'] = labs_csv(patient, count)
            fmt = 'csv_text'
            for visit in range(count):
                day = (SOURCE_DAY + timedelta(days=visit * 100)).isoformat()
                files[f'{label}__visit-{visit}.txt'] = worksheet(patient, visit, day).replace('DOS:', 'Service date:').encode()
        entries.update(files)
        mapping.update({name: label for name in files})
        patients.append({'label': label, 'visits': count, 'primary_format': fmt, 'files': sorted(files)})
    entries['manifest.json'] = json.dumps({'specialty': 'nephrology', 'deidentified': True,
                                          'note_type': 'Visit worksheet', 'files': mapping}, sort_keys=True).encode()
    archive(ROOT / 'mixed_patients.zip', entries)
    unmapped = {}
    for patient in range(1, 4):
        unmapped[f'chart-{patient}.xml'] = ccda(patient, 3)
        unmapped[f'worksheet-{patient}.pdf'] = pdf(patient, 3)
    archive(ROOT / 'unmapped_patients.zip', unmapped)
    manifest = {'synthetic': True, 'provenance': 'hand-authored; no source patient records or Synthea output',
                'patients': patients, 'archives': {}}
    for path in [ROOT / 'mixed_patients.zip', ROOT / 'unmapped_patients.zip', ROOT / 'qualification_patients.zip']:
        manifest['archives'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (ROOT / 'fixture_manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    generate()
