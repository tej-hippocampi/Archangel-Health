# ehr-1 — De-identified EHR export

Single patient longitudinal record derived from photographed clinical documents.

## Contents

| Path | Format | Description |
|------|--------|-------------|
| `fhir/Bundle-ehr-1.json` | FHIR R4 Bundle | Patient, Conditions, Observations, DiagnosticReports, DocumentReferences, MedicationStatements, Composition |
| `fhir/Patient-ehr-1.json` | FHIR R4 Patient | Anonymous patient resource |
| `hl7v2/ORU_R01_*.hl7` | HL7 v2.5 ORU^R01 | Lab result messages grouped by service date |
| `labs/lab_results.csv` | CSV | Flat lab results with LOINC where mapped |
| `clinical-notes/` | Plain text | Summary, per-document notes, radiology/ICU/meds compilations |

## Anonymization

- No patient/relative names, phone numbers, MRNs, national IDs, emails, addresses, or facility/provider names.
- Patient ID: `ehr-1-patient` (synthetic).
- Sex: male; age (masked): 39Y. Exact DOB never recorded; ages >89 reported as `89+`.
- Clinical dates are de-identified consistently so longitudinal intervals remain intact.

## Clinical synopsis

Male patient with noncirrhotic portal hypertension due to chronic portal vein thrombosis
(cavernous transformation), portal biliopathy, cholelithiasis, splenomegaly with thrombocytopenia,
esophageal varices (EVBL), and recurrent CBD stenting via ERCP. Hospitalized for
post-ERCP pancreatitis; later ICU admission for septic shock secondary to cholangitis.

Source images processed: 176/176.
