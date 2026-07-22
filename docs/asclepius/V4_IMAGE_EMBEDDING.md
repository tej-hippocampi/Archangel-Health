# V4 Image Embedding & Analysis (real de-identified imaging)

**Scope: V4 only** (`case_source='real_deid'`). Adds real medical images (ECG strips,
echo/CT/PET stills, pathology region images) as a first-class case modality that
**both frontier models analyze** and the physician annotates over. Additive — V1/V2/V3
and text-only V4 are untouched.

## Data model
A `Study` (on `ClinicalCase.studies`) may carry an optional `asset: StudyAsset`:

```
StudyAsset { asset_id, mime, sha256, width, height, byte_size, page, page_count, source }
```

The image **bytes are never** stored on the `ClinicalCase` or in `asclepius.db` — only
this reference. `findings` stays required even with an image (the reasoning anchor +
the fallback if a model can't see the image). `public_case` ships `asset`;
`assert_multimodal_content` accepts an image-bearing study (valid asset + non-empty
findings) as satisfying the multimodal requirement.

## Asset store (`asclepius/assets.py`)
Content-addressed by `sha256`, configured by `ASCLEPIUS_ASSET_STORE` (local filesystem
path by default; `s3://` reserved). Ingest (`process_upload`):

1. **Accept PNG/JPEG/PDF only** (else `415`); enforce `ASCLEPIUS_IMAGE_MAX_BYTES`
   (25 MB) and downscale over `ASCLEPIUS_IMAGE_MAX_DIM` (4000 px longest edge).
2. **PDF → raster** for viewing + vision (page 1 default; page count recorded).
3. **Metadata hygiene strip** — EXIF/XMP/ICC-beyond-color/GPS/device/timestamps/
   thumbnails removed by re-encoding to a clean PNG/JPEG. *Not* a de-identification
   check; the partner attestation is trusted (§9).
4. **Hash + store** content-addressed, dedupe on `sha256` (identical image costs once).

## Endpoints
- `POST /api/asclepius/assets/ingest` (admin): upload → attach a `StudyAsset` to a V4
  case's study. Enforces the V4 wall (`400` on a non-`real_deid` case). Returns the
  reference only — never bytes, store path, or partner id.
- `GET /api/asclepius/assets/{asset_id}` (evaluator/admin): streams the cleaned image
  with cache headers. No provider/model/partner identity; the store path is never
  exposed. The evaluator payload references the asset by `asset_id` only.

## Two-frontier vision A/B (`baselines.py`)
Both candidates are produced by **vision-capable** models reading the **identical**
image (`is_vision_capable` preflight — a non-vision model degrades to `needs_baseline`,
never a silent text-only grade). One asset → two identical payloads (Anthropic base64
block; OpenAI Responses/chat image parts, via `ai/llm_client.py`). The `prompt_hash`
folds in the image `sha256` (`sha(system + rendered_case + image_sha256)`), so the
existing pair-divergence guard also discards a pair that somehow received different
images. The shared baseline system tells the model the **image is authoritative**.

`ASCLEPIUS_TWO_FRONTIER_V4` gates sending real de-identified images to OpenAI — a
logged founder/compliance decision (OpenAI is in the subprocessor register,
`compliance/subprocessors.py`, PHI-ineligible until a BAA). For an image case the PRD
requires it ON; running without it logs the explicit decision and uses the
lower-value Anthropic-only vision pair.

## Grading (`grader_eval.py`)
On an image case the grader **receives the same image** the models saw (else its
validity measurement is on a different input). If no vision grader is available the
grader block is marked `skipped` with reason `vision_grader_unavailable` — never a
text-only grade called validated.

## Export (`export.py` §8)
The cleaned image bytes are bundled with the record set under `assets/`, referenced by
`asset_id` + `sha256` (integrity). The manifest lists `image_assets` with
`{asset_id, sha256, modality, mime, license, provenance, path}`. `case_type` reflects
the image modality (e.g. `multimodal:real+ecg_image`). Blinding holds — stripped
images only, no provider/model/partner identity.

## Viewer (frontend §6)
`renderCasePanel` shows the image inside its study tab, **above** the findings, with
zoom (scroll / ＋－), pan (drag), fit/reset, full-screen, keyboard control
(＋/－/0, arrows, Esc), multi-page PDF navigation, a loading skeleton, and a real
load-failure state (never a broken-image icon). Tokens only; respects
`prefers-reduced-motion`.

## PHI hygiene / risk note (§9)
The metadata strip is data hygiene, not de-identification. Trusting the partner
attestation means a residual identifier burned into a pixel would pass through — an
accepted risk per the locked founder decision. `ASCLEPIUS_IMAGE_BURNIN_SCAN` (default
**off**) is an optional OCR backstop that **flags** (never blocks) a suspicious image
for admin review.

## Config
| Env | Default | Purpose |
|---|---|---|
| `ASCLEPIUS_ASSET_STORE` | `asclepius/_assetstore` | content-addressed store path |
| `ASCLEPIUS_IMAGE_MAX_BYTES` | 25 MB | upload size cap (`413`) |
| `ASCLEPIUS_IMAGE_MAX_DIM` | 4000 | longest-edge px cap (downscale) |
| `ASCLEPIUS_IMAGE_KEEP_PDF` | off | keep the original PDF beside the raster |
| `ASCLEPIUS_IMAGE_BURNIN_SCAN` | off | OCR backstop — flags, never blocks |
| `ASCLEPIUS_TWO_FRONTIER_V4` | off | send real images to OpenAI (vision A/B) |
| `OPENAI_BAA_SIGNED` | off | flip when a BAA with OpenAI is executed |
