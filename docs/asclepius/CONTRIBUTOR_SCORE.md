# The contributor score

What the number next to a physician's name means, where it comes from, and why
it moves. Written because a test signup filled the credential form with
keyboard noise and scored 29 out of 100 — high enough to look like a marginal
doctor rather than a fake one, and nobody could say from the product why.

## The short version

Two numbers, and they are different things.

**The initial rating** is what we can tell about a physician before they have
done any work: identity confirmed against a registry, a corroborated board
certification, years in practice, the domain their email comes from. It is
computed by `credentialing.propose_tier` and lives in `users.tier_score`.

**The contributor score** is what their work says. It starts at the initial
rating and is pulled toward the quality of the cases they actually complete,
in `asclepius/contributor_score.py`. After enough graded cases, what they did
outweighs what they claimed, which is the entire point.

Nothing here assigns a tier. The score proposes; an admin decides, and the
approval endpoint requires an explicit tier in the request body.

## The initial rating

Weights, from `credentialing.TIER_WEIGHTS`:

| Signal | Weight | Condition |
|---|---|---|
| Identity verified | +25 | NPI matched in NPPES, **or** a registration confirmed by that country's registry |
| Board certified | +20 | The text reads as a board certification, or the CV corroborates it |
| 10+ years in practice | +20 | |
| 5–10 years | +10 | |
| Academic email | +10 | |
| Health-system email | +10 | A known employer domain |
| Business email | +6 | |
| Consumer email | −4 | Not disqualifying; plenty of practising doctors use Gmail |
| Specialty matches the registry | +10 | NPPES taxonomy maps to a specialty we serve |
| CV parsed | +5 | |
| LinkedIn | +3 | Must actually be a LinkedIn profile URL |

Thresholds: **70** proposes reviewer (with identity established), **30**
proposes labeler, below that no proposal — which is not a rejection, it is
"a person should look at this".

Identity is worth the same wherever it was established. A doctor confirmed
against SCFHS or the Indian Medical Register has proved exactly what an NPPES
match proves. Reviewer used to require an NPI specifically, which capped every
international physician at labeler no matter how well they checked out.

## What stops nonsense scoring

Before `asclepius/plausibility.py`, the weights only asked whether a field was
non-empty. `"n;n"` is a truthy string, so it earned the full twenty points for
board certification, and `"nlk"` earned three for a LinkedIn profile. That is
how the test signup reached 29. It now scores 6, with seven findings against
it.

Two questions, deliberately separate:

- **Can this value be what it claims to be?** If not, the weight is not
  awarded, and the reason says so in words an admin can read.
- **Should a human be told?** High-severity findings become blockers, which
  suppress the proposal and force review — the same mechanism an NPI mismatch
  uses. **A flag is never a rejection.**

What is checked: board certifications that name no board and no specialty;
LinkedIn values that are not LinkedIn URLs; licence numbers with no digits;
US licence states that are not states; years that cannot happen (7689) and
timelines that cannot happen (residency finishing before the degree); more
years of practice than time since qualifying; registration numbers in an
unusual shape for the country that issued them.

### The harder half

Not calling real doctors nonsense. The first draft got three cases wrong, and
they are pinned in `tests/test_plausibility.py`:

- **Björk** was flagged because splitting on ASCII letters cut the word at the
  umlaut and left vowel-free fragments. Accents fold first now.
- **FRCPC** was flagged because a real credential acronym has no vowels.
  Acronyms are allowed.
- **National Board of Examinations — DNB Nephrology** was flagged because
  joining its words manufactured a consonant run (`nsdnbn`) that none of its
  words contains. Words are judged one at a time.

A licence number gets a different test entirely: it is a code, not language.
`A94021` is a fine licence and a terrible word.

The rule to keep: **a missed flag costs a glance, a false flag tells a real
doctor their name looks fake.** When in doubt, do not fire.

## What the work says

Per graded case, from `contributor_score.py`:

```
outcome        accepted 85 · accepted with edits 70 · rejected 30
citations      +1 per evidence anchor, capped at +5
reasoning      +0.5 per step, capped at +5
agreement      clamp(10 × (kappa − 0.5), −5, +5)
time           +3 when the time taken is sane, −5 when implausibly fast
```

Blended against the initial rating with a prior weight of 5, so roughly five
cases in, the work is worth as much as the paperwork:

```
score = (5 × initial_rating + Σ case_scores) / (5 + n_cases)
```

Bands: **70+** reviewer band, **30+** labeler band, below that "Building".

## What must never be scored

`docs/PRD_C_COUNSEL_MEMO.md` §3.3 lists what may not be collected, derived or
logged: medical school name or rank, US-MD versus IMG, ECFMG certification as
a score input, graduation year, date of birth, sex, continuous years in
practice, practice ZIP, self-rated expertise.

International onboarding added country of practice, country of licensure,
country of degree, the registration number and the qualification. Country is
one step from IMG status, so all of them are pinned immobile at the encoder
alongside it — `tests/test_tiering_score.py` varies each across its plausible
range and asserts the score moves by exactly 0.0. **Country routes
verification. It never scores.**

## Worked examples

**The gibberish signup** — business email (+6), board certification `"n;n"`
(±0, does not read as one), LinkedIn `"nlk"` (±0, not a URL), NPI not found
(±0). **Score 6, no proposal, seven blockers**, admin review.

**A US nephrologist** — NPI verified (+25), ABIM nephrology (+20), 14 years
(+20), stanford.edu (+10), taxonomy matches (+10), LinkedIn (+3).
**Score 88, proposes reviewer.**

**A Saudi consultant** — SCFHS registration confirmed (+25), Saudi Board of
Internal Medicine (+20), 11 years (+20), hospital academic domain (+10).
**Score 75, proposes reviewer.** Identity via document review instead would
score 50 and pending, which is not a mark against them: it is an unfinished
check, and it says so.
