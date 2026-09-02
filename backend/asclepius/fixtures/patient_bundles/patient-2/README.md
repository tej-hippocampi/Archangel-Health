# patient-2 — De-identified EHR export

Single patient longitudinal record derived from photographed clinical documents
in `Patient M data/`.

## Contents

| Path | Format | Description |
|------|--------|-------------|
| `fhir/Bundle-patient-2.json` | FHIR R4 Bundle | Patient, Conditions, Observations, DiagnosticReports, DocumentReferences, MedicationStatements, Composition |
| `fhir/Patient-patient-2.json` | FHIR R4 Patient | Anonymous patient resource |
| `hl7v2/ORU_R01_*.hl7` | HL7 v2.5 ORU^R01 | Lab result messages grouped by service date |
| `labs/lab_results.csv` | CSV | Flat lab results with LOINC where mapped |
| `clinical-notes/` | Plain text | Summary, per-document notes, radiology/clinical/meds compilations |

## Anonymization

- No patient/relative names, phone numbers, MRNs, national IDs, emails, addresses, or facility/provider names.
- Patient ID: `patient-2-patient` (synthetic).
- Sex: male; age (masked): 65Y. Exact DOB never recorded; ages >89 reported as `89+`.
- Clinical dates are de-identified consistently so longitudinal intervals remain intact.

## Clinical synopsis

Diabetes mellitus; Hypertension; Chronic liver disease / cirrhosis features; Portal hypertension / portal vein abnormality; Suspected hepatocellular carcinoma; Ascites; Hepatic coarse echotexture; Viral hepatitis; Cholelithiasis; Esophageal / gastric varices; Jaundice / biliary obstruction; Splenomegaly

Source images processed: 134/134.
