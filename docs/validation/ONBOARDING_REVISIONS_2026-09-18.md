# Specialty onboarding release validation

## Result and scope

The prepared library contains 86 reviewed cases: one practice case and one
examination case for each of 43 supported specialties. This change adds the 45
missing bundles and preserves the original 41 files byte-for-byte. The existing
specialty routing selects the prepared pair without waiting for live generation.
Existing physician draws, submitted examinations and paid batches are preserved.

Cases use synthetic patient scenarios and public clinical evidence. Pathology
uses distinct, public-domain de-identified reference fields with synthetic
scenarios, explicit single-field limits and blinded pixel review. No health-system
partner material or applicant/CV information was used to author these cases.

## Review and release protocol

New bundles passed blinded solves and clinical reviews from GPT-5.6 Sol and
GPT-5.5, literal evidence verification and separate fresh-context artifact audits.
This is automated review, not physician ratification. Both models share one
provider and may have correlated errors; model agreement alone does not authorize
release. Ordinary runtime generation still requires two different providers.

Original trial reports retain `method: openai_only_trial`. The explicit
`audited_openai_models_v1` release envelope requires all of the following:

- The complete document matches its registered checksum.
- The immutable independent report matches its checksum and clears the exact
  task ID and entry checksum.
- The two distinct model/provider identities are valid; blinded answers agree
  with the key at confidence >=0.90 and all clinical review gates pass.
- Every claim is supported by literal retained passages from every cited source;
  public answer leakage, identifiers and pathology pixel failures block release.

Cached lookups recheck audit authority, so a revoked case cannot enter a new draw.
Raw trial output remains ineligible and never writes the onboarding bank. Rejected
artifacts and their actual verdicts are retained; none were relabelled as passes.

## Revisions and evidence

Revisions repaired evidence gaps, missing clinical context and public answer cues.
Examples include assessment-specific pathology morphology; neutral palliative
imaging findings; coherent oncology risk-score interpretation; appropriately
scoped vascular and rehabilitation decisions; and optional, evidence-supported
infectious-disease imaging without unrelated treatment instructions.

Twenty-three exact evidence pins recover applicable guidance or reviews
misindexed in PubMed. Independent source audits verified identity, publication
year, population, retraction status and actual retained passages. They distinguish
primary guidelines, systematic reviews and secondary clinical reviews. A 2019
vascular review is used only for supported emergency anticoagulation/team advice.
The nuclear-medicine audit records the publisher's glucose-unit correction;
patient-facing case content uses the corrected mg/dL expression.

Retained full text rejects conflicting or restrictive reuse labels, including a
restrictive human-readable license paired with a CC BY link. Separately credited
third-party tables, figures and containing paragraphs are excluded. The ENT query
uses mass/masses explicitly to avoid neck-massage retrieval. The radiology topic
matches the evidence-backed initial CT characterization decision.

The last five cases use checksum-pinned complete drafts, reviewed without model
reauthoring. Inputs are validated before paid probes; canonical schema changes
are rejected. The author call alone is skipped: four fresh blinded/clinical calls
remain, with all existing safety gates. Metadata truthfully records a null API
author and `authoring_method: prepared_revision`. Prior feedback never reaches
fresh reviewers. The final infectious-disease correction narrows recommendations
to source evaluation and attributes compound claims only to supporting evidence.

## Actual clinical runs and artifact audits

- [Initial OpenAI run 35412250353](https://github.com/tej-hippocampi/Archangel-Health/actions/runs/35412250353):
  18 model passes; 16 cleared independent audits and two were held for revision.
- [Revision run 35415041635](https://github.com/tej-hippocampi/Archangel-Health/actions/runs/35415041635):
  16 revised cases passed and cleared independent audits.
- [Revision run 35417109660](https://github.com/tej-hippocampi/Archangel-Health/actions/runs/35417109660):
  eight revised cases passed and cleared independent audits.
- [Prepared-draft run 35419124341](https://github.com/tej-hippocampi/Archangel-Health/actions/runs/35419124341):
  four cases passed and cleared independent audits; infectious diseases was held.
- [Final infectious-disease run 35419404122](https://github.com/tej-hippocampi/Archangel-Health/actions/runs/35419404122):
  the corrected case passed both fresh reviews and its independent artifact audit.

All runs used isolated CI stores, GitHub-held OpenAI credentials, and one attempt
per selected case. Exact accepted artifacts and reports are linked by
`onboarding_material/openai_release_audits.json`; reports reside in `audits/`.
Source and code audits reside in `source_audits/`. Final artifact audits verified
104 literal quote instances across the final five cases, exact prepared inputs,
40 retained sources, 20 fresh review calls and distinct practice/examination pairs.

## Software and preservation verification

Final focused suite: **272 passed**, including complete 86-case coverage.
The frozen before/after file inventory validates all 86 bundles and preserves
every original hash; `source_audits/release-file-inventory.json` records the result.
The completeness assertion remains unchanged; it previously caused the two red
software shards while the library was incomplete. Required software CI must pass
on the final commit before merge.

Fresh independent code audits found and confirmed fixes for provenance relabeling,
stale cached audit authority, malformed audit records, Python-equal schema
coercions and paid probes preceding input validation. Tests cover changed clinical
or source content, duplicate reviewers, missing pixel review, bad citations,
changed audit reports and provider failure without publication.

The frozen 41-file inventory remains exact. No migration, deployed-store write,
email or payment is part of this release. These artifact checks do not verify
ongoing production backup or alert coverage. See the dated data-safety record.
