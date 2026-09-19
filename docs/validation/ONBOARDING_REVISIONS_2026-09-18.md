# Onboarding case revision evidence

## Release protocol

The deployed baseline contains 41 immutable, cross-provider-reviewed cases. Their
original bytes are preserved. New prepared cases use two distinct OpenAI models
(GPT-5.6 Sol and GPT-5.5), blinded solves, literal source verification, all clinical
safety checks, and separate artifact review. This is automated review, not
physician ratification. Both models share one provider; their errors can remain
correlated, which is why model agreement alone does not authorize release.

Original trial validation reports retain `method: openai_only_trial`. The explicit
`audited_openai_models_v1` release envelope is accepted only when the whole
document matches the audit registry, the referenced independent report matches
its checksum, and that report clears the exact task ID and entry checksum.
Two exact distinct models, matching provider provenance, safe/correct answers,
>=0.90 confidence, literal quotations, and pathology pixel review remain required.
Ordinary runtime generation still requires two different providers. Cached
lookups recheck audit authority so a revoked case cannot enter a new draw.
Existing completed draws and stored ready cases remain preserved.

## Initial independent review

Three fresh-context artifact audits verified 18 preliminary OpenAI passes.
Sixteen cleared; two were held: a palliative CT impression provided the tested
prognostic interpretation, and an ENT citation did not inform the diagnostic
question. Those two returned to authoring alongside 27 original rejections.

The first four revised cases passed both model reviews and separate artifact
audits: dermatology examination, pediatrics examination, neurology examination,
and pathology practice. Pathology now has a distinct audited pair, each using a
synthetic scenario and a pinned public-domain, de-identified reference field.
No health-system partner material was used.

## Software and provenance checks

214 focused software checks passed, with the full 86-case completeness test
separately blocked until the remaining reviewed artifacts are committed.
Independent release-gate review found and confirmed fixes for provider relabeling,
stale cached audit authority, and malformed provenance records. Regression checks
cover those failures, changed clinical/source content, duplicate model selection,
missing pixel review and altered audit reports.

Seven exact evidence pins were independently verified against actual PubMed and
Europe PMC metadata. Ninety additional read-only checks confirmed identity,
recency, retraction, population, license and retained-text behavior. Guidance
misindexed as narrative reviews is accepted only through these explicit pins;
noncommercial/no-derivatives/share-alike full-text licenses remain excluded.

Original case and trial artifacts are retained. The initial import audit verified
all 41 original file hashes and unchanged entry/validation contents for the 16
newly released envelopes. These local fixture checks do not verify ongoing
production backup or alert coverage.

## Current real-model run

[Revision run 35415041635](https://github.com/tej-hippocampi/Archangel-Health/actions/runs/35415041635)
uses commit `135b8cb5be3888b469c31102bfbee3f80affd1ce`, isolated empty CI stores,
OpenAI credentials held only in GitHub Secrets, and one attempt per queued case.
It skips the 16 audited initial passes. Both review outcomes and safe rejection
diagnostics are retained. Further iterations must address specific defects and
receive fresh reviews; no failing verdict is changed into an approval.
