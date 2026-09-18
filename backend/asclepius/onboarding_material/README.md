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
examination, checks independence, and uploads only passing cases. Existing
committed cases are reused; building never overwrites their identities. A failed
case remains unavailable, and a successful companion is retained as an artifact.
Real model keys remain in GitHub Secrets. No physician/CV/partner data is used.

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
