# Onboarding teaching material

This backend-only directory contains the fixed onboarding curriculum's released
case pairs and the two pinned pathology reference micrographs. It is not mounted
as a public static directory and is never a source for paid annotation exports.

## Provenance

Patient scenarios are entirely synthetic. The pathology images are **real,
public-domain reference micrographs**, not images from those fictional patients
and not health-system partner data. They are single fields, not whole slides.

Both images are by Mikael Häggström, M.D., released under CC0 1.0:

- [Practice image source](https://commons.wikimedia.org/wiki/File:High-magnification_micrograph_of_basal-cell_carcinoma.jpg)
- [Examination image source](https://commons.wikimedia.org/wiki/File:Ulcer_border_of_a_squamous_cell_skin_cancer.jpg)
- [CC0 terms](https://creativecommons.org/publicdomain/zero/1.0/)

The source pages explicitly state the absence of identifiable features. Local
visual inspection found tissue only, without names, labels or patient identifiers.
Images were decoded to RGB PNG with all original metadata discarded. There was
no cropping, diagnostic markup or modification to tissue morphology. Source and
served-byte SHA-256 hashes, license records and the inspection note are in
`image_sources.json`. Original source filenames and diagnostic captions are not
sent to applicants. Both independent clinical reviewers must inspect the served
image bytes, agree with the answer, and confirm no identifiers are visible.

## Build and release

The manual **LLM smoke (real models)** workflow's `onboarding-library` mode accepts
one launch specialty or `all` (43). Each isolated job prepares practice before
examination, checks independence, and uploads passing cases separately from
rejected-attempt diagnostics. Existing
committed cases are reused; building never overwrites their identities. A failed
case remains unavailable, and a successful companion is retained as an artifact.
Real model keys remain in GitHub Secrets. No physician/CV/partner data is used.

The library workflow uses the existing OpenAI model (`OPENAI_MODEL`, default
`gpt-5`) for authorship through `MODEL_ASCLEPIUS_CASE_GEN`. Anthropic and OpenAI
still independently solve and review each new case. This override is confined to
the batch build; it does not change production or paid-case generation defaults.
Both provider keys are still required, and provider failures never bypass review.

The CI-only diagnostic directory retains structurally valid, identifier-screened
rejected attempts with both providers' outcomes, request IDs, exact blinded input,
source hashes and any pinned image hash. Files begin with `rejected-`; they cannot
be loaded as released cases. Both reviews finish even when one rejects. Production
has no diagnostic file writer. Invalid authored entries are not retained; an
identifier rejection logs only its category, never the matched text.

Before importing passing artifacts, also inspect the applicant-visible case for
answer leakage and incomplete recommended care. A note that states the intended
plan invalidates the assessment even when both model reviews approve it. Keep
rejected material outside `cases/`; never manually edit a reviewed entry to fix
it, since that would invalidate its content hash and clinical review.

New clinical reviews must quote each cited source. The server verifies that each
excerpt occurs in the retrieved text; reviewers separately judge whether it
establishes the recommendation for the actual population. Scope summaries,
remembered guideline details and absence of contradictory evidence do not count.
Reviewers also check decisive advice omitted from the author's claim list.

Seven pre-protocol artifacts received an independent audit against their actual
source text. `legacy_evidence_audits.json` pins each entire original document,
including its sources and provider reports, by SHA-256. It does not add invented
quotes to old provider responses. Every other artifact requires the new protocol;
removing its marker or changing any legacy document rejects it.

Review/download successful artifacts into `cases/`, then run:

```sh
python backend/scripts/build_onboarding_library.py --check
```

The release coverage test requires all **86** reviewed entries. Software fixtures
are not clinical approval. Runtime validates schema, content hashes, evidence
reviews, independent blinded agreement and (for pathology) pixel review before
serving bundled material. Existing ready database cases take precedence and are
never overwritten. Legacy gold batches, accepted submissions and completed exam
snapshots are retained. Retakes beyond the prepared pair use the existing
separate generation bank with the same clinical gates.

Automated review is not physician ratification. These materials assess onboarding
skills; they are not clinical care recommendations or saleable annotations.
