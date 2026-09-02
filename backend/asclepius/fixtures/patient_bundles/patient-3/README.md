# patient-3 — De-identified EHR export

Single patient longitudinal record derived from photographed clinical documents
in `Patient data 187 A/`.

## Contents

| Path | Format | Description |
|------|--------|-------------|
| `fhir/Bundle-patient-3.json` | FHIR R4 Bundle | Patient, Conditions, Observations, DiagnosticReports, DocumentReferences, MedicationStatements, Composition |
| `fhir/Patient-patient-3.json` | FHIR R4 Patient | Anonymous patient resource (no DOB) |
| `hl7v2/ORU_R01_*.hl7` | HL7 v2.5 ORU^R01 | Lab result messages grouped by shifted service date |
| `labs/lab_results.csv` | CSV | Flat lab results with LOINC where mapped |
| `clinical-notes/` | Plain text | Summary, per-document notes, radiology/clinical/meds compilations |

## Anonymization

- No patient/relative names, phone numbers, MRNs, national IDs, emails, addresses, or facility/provider names.
- Patient ID: `patient-3-patient` (synthetic).
- Sex: female; age (masked): 76Y. Exact DOB never recorded; ages >89 reported as `89+`.
- Clinical dates are de-identified consistently so longitudinal intervals remain intact.

## Clinical synopsis

Anemia; Chronic liver disease / cirrhosis features; Ascites; Splenomegaly; Hypertension; Hepatic coarse echotexture; Diabetes mellitus; Jaundice / biliary obstruction

Source images processed: 93/93.
