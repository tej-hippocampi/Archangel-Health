# ASCLEPIUS FOR PHYSICIANS

**Version v1**

<!--
  THIS FILE IS THE SOURCE OF THE ONE-PAGER THAT GOES OUT WITH THE APPLICATION
  LINK AFTER A FOUNDER'S INTRO CALL. It is not documentation about the
  one-pager. It is a sibling of docs/legal/PHYSICIAN_AGREEMENT_v1.md and follows
  the same rules, for the same reasons:

  1. THE DOCUMENT IS A FILE SO THAT CHANGING IT IS A CONTENT CHANGE. Rewriting
     the pitch is editing this markdown, not editing Python and redeploying a
     hand-built PDF. asclepius/one_pager.py renders it; nothing else knows what
     it says.

  2. VERSIONS ARE ADDITIVE. A new version is a new file,
     docs/asclepius/PHYSICIAN_ONE_PAGER_v2.md, plus a bump of
     asclepius/one_pager.CURRENT_VERSION. The old file stays, because a
     follow-up already sent named the version it carried and a reader who
     opens that link later should get the document they were sent.

  3. NO NUMBER APPEARS HERE THAT THE PRODUCT CANNOT STAND BEHIND. There is no
     per-case rate in the codebase (see asclepius/compensation.py: the accrual
     seam exists, the rate does not), so no rate is stated. A one-pager that
     quotes a number nobody has decided is how a physician arrives at their
     first payout expecting something else.

  4. NO PHI, EVER. This document goes to people who have not signed anything.

  HTML comments are stripped before rendering, so nothing in this block reaches
  a reader.
-->

Archangel Health builds the expert medical data that AI systems are evaluated
and trained against. Asclepius is the product physicians work in: you read real,
de-identified clinical material, judge what an AI system got right and wrong,
and write down why. Your judgment becomes the reference standard.

---

## Why this exists

A model that scores 70 percent on a medical benchmark is not safe to put in
front of a patient, and the benchmark cannot tell you which 30 percent it missed
or how badly. The people who carry the consequences of a wrong call are the only
people who can say what correct means. That is the whole thesis, and it is why
the work is done by practising clinicians rather than by annotators reading a
rubric.

---

## What the work actually is

You are shown a case and one or more AI-generated answers to it. You do three
things:

**Judge.** Say whether the answer is clinically correct, and where it is wrong,
say what it got wrong and what the consequence would have been.

**Correct.** Write the answer you would give, in your own words, at the level of
detail you would use with a colleague.

**Attest.** Confirm that the case is clinically coherent and that your judgment
is your own. Every case you complete carries that attestation.

Cases are routed to your specialty. You take the ones you want and skip the ones
you do not. There is no minimum volume, no shift, and no schedule.

---

## What we ask of you

- An active clinical licence, which we verify before you are approved.
- Your own independent professional judgment on every case. You do not delegate
  platform work to anyone else.
- One practice case before approval, so we both find out early whether this is
  the kind of work you want to be doing.

---

## What you get

- Paid work you can do from anywhere, in blocks as short as one case.
- A signed contributor agreement that names what you have agreed to, and a
  countersigned copy you keep.
- Credit for the standard you help set, and a direct line to the two founders.
  We meet every physician one on one, and we keep meeting them.

---

## Confidentiality and patient data

Everything you see on the platform is de-identified before it reaches you. You
will not be asked to supply patient data to work on cases, and you must not
enter identifiable patient information into the platform. Material on the
platform is confidential and stays there.

---

## What happens next

The email this document came attached to has your application link. It takes
about fifteen minutes: your identity, your credentials, the contributor
agreement, and one practice case. We review every application by hand and come
back to you.

If anything here is wrong for you, tell us on the thread. We would rather hear
it than not.

---

Archangel Health Inc. Written for physicians considering contributing to
Asclepius. Not medical advice, not an offer of employment, and not a
solicitation of patient data.
