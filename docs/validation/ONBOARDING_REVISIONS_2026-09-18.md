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

## Second targeted revision

The first revision run completed 29 attempts: 16 passed model review and 13 were
rejected. All 16 passes subsequently cleared independent artifact audits, bringing
the prepared library to 73 of 86. The 27 original rejections plus 2 independently
held model passes have not been silently approved or removed from coverage.

The remaining 13 inputs retain their latest safely validated synthetic draft and
review findings. Revisions narrow unsupported treatment rankings, remove public
answer cues and use exact substantive sources for the assessed decision. Source
audits are retained in `onboarding_material/source_audits`. They distinguish
primary guidelines, systematic reviews and secondary clinical reviews, and do not
confer publication authority on any case. The nuclear-medicine audit records the
publisher's corrected glucose unit; the case must not reproduce the typo.

Retained full text now rejects a restrictive human-readable license even when a
link says CC BY. Separately credited third-party tables or figures and containing
paragraphs are excluded; article-level licensing does not grant their reuse. The
ENT query now uses mass/masses explicitly so it cannot expand to neck massage.

Latest focused suite: **249 passed**, with the 86-case completeness gate separately
held until all artifacts are approved. Public-reference preflight: **13/13 available**.

Independent retention-code audit: clear after fixing credit-only table/figure
notices; 71 focused tests, 76 independent rejection probes and five intact-source
compatibility fixtures passed. The immutable report is retained with source audits.

Second-round input audit: ready for authoring CI, no open blockers. It verified
all 13 input hashes, unchanged resume rows, fresh source bodies/abstracts, exact
pins and original bundle hashes. Removed unrelated ENT fallback sources and
aligned the radiology curriculum with the evidence-backed initial CT decision.
This input audit does not clear any future generated artifact for release.

## Exact-draft reviews and final five revisions

[Second revision run 35417109660](https://github.com/tej-hippocampi/Archangel-Health/actions/runs/35417109660)
completed 13 attempts: eight passed both model reviews and independently cleared
artifact audits, bringing the library to 81/86. The two failing software shards
failed only the unchanged 86-case completeness assertion. Other software checks
passed; clinical rejections are retained for the five remaining drafts.

The last five drafts are revised explicitly: vascular and rehabilitation claims
are scoped to their actual supporting evidence; pain-medicine public answer cues
are removed; oncology assesses risk-score interpretation rather than an
underspecified discharge instruction; infectious-disease clinical details clarify
the ongoing source evaluation decision. These are inputs, not approved cases.

CI can now review a checksum-pinned prepared draft without a model reauthoring it.
It requires a complete, unchanged schema and validates all inputs before paid
access probes. Four fresh calls remain: independent blinded solve and clinical
review by each of the two OpenAI models. Source, privacy, confidence, safety,
public-answer-boundary and image checks remain unchanged. The prepared path
records a null API author and truthful authoring method; trial output remains
ineligible for release without a separate checksum-bound artifact audit.

Independent code review confirmed fixes for Python-equal numeric/boolean schema
coercions and paid CLI probes preceding input checks. Current focused validation:
**271 passed**, with only full-library completeness deliberately held while the
last five cases await review. Original 41 bundle bytes remain unchanged.
