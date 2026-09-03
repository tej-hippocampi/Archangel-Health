# PHYSICIAN CONTRIBUTOR AGREEMENT

**Version v1 (interim)**

<!--
  THIS FILE IS THE SOURCE OF THE AGREEMENT A PHYSICIAN SIGNS IN THE FIRST STEP
  OF ONBOARDING. It is not documentation about the agreement. It is a sibling of
  docs/legal/DLA_v1.md and follows the same three rules, for the same reasons:

  1. EDITING THIS FILE CHANGES ITS SHA256, AND THE SHA256 IS WHAT WAS SIGNED.
     Every signature row records the hash of the exact text that was on the
     signer's screen. A typo fix here does not retroactively change what anyone
     agreed to. Fix a typo by shipping v2 (a new file,
     docs/legal/PHYSICIAN_AGREEMENT_v2.md, and a bump of
     asclepius/physician_agreement.CURRENT_VERSION); do not edit a version
     anyone has signed. Shipping v2 is what puts every physician who signed v1
     back in front of the agreement before they draw another case.

  2. The {{PLACEHOLDERS}} below are substituted per physician at render time by
     asclepius/physician_agreement.py. Adding one means teaching that module
     about it; an unknown placeholder is left in the text on purpose, so it
     shows up on screen rather than silently rendering as an empty space in a
     contract.

  3. THIS IS THE INTERIM VERSION AND IT SAYS SO ON ITS FACE. The operative
     language is an external dependency: Gunderson is supplying it. What is
     below is the seven attestations the product has always collected, assembled
     into one readable document so that "what exactly did this physician agree
     to, and when" has an answer that is a document rather than seven booleans
     in a JSON blob. Swapping counsel's language in is a content change to this
     file plus a version bump, and touches no Python.

  HTML comments are stripped before rendering, so nothing in this block ever
  reaches a signer.
-->

**This is an interim agreement.** Archangel Health Inc. is having this document
prepared by outside counsel. Until that version is issued, the terms below are
the terms, and they are the same commitments the signup form has always asked
for, stated in one place. When counsel's version is issued it will be published
as a new version and you will be asked to read and sign it before your next
case. Nothing you have already done, and nothing you have already earned, is
changed by that.

This Physician Contributor Agreement (this "**Agreement**") is entered into as
of {{EFFECTIVE_DATE}} (the "**Effective Date**") by and between:

**{{PHYSICIAN_NAME}}**, an individual licensed clinician ("**Contributor**"), and

**Archangel Health Inc.**, a Delaware corporation ("**Archangel**").

Contributor and Archangel are each a "**Party**" and together the "**Parties**".

---

## 1. What this Agreement covers

**1.1** Archangel operates a platform on which licensed physicians and other
clinicians review, annotate, evaluate and adjudicate de-identified clinical
material, and licenses the resulting material to third parties, including
developers and evaluators of artificial intelligence systems.

**1.2** Contributor wishes to perform that work and to be paid for it.

**1.3** This Agreement is signed once, before any labeling, and governs every
case Contributor works on until it is superseded by a later version.

**1.4** This Agreement does not create an employment relationship. Contributor
performs work as an independent contractor, sets their own hours, and is under
no obligation to accept any particular case or any minimum volume of work.

---

## 2. Independent professional judgment

**2.1** Contributor attests that the labels, corrections, rankings and written
judgments they produce on the platform reflect their own independent
professional judgment as a licensed clinician.

**2.2** Contributor will not delegate platform work to another person, and will
not submit work produced by an automated system as their own judgment.

---

## 3. Clinical validity, and what attesting to it means

**3.1** Some cases on the platform are synthetic, and some are derived from real
de-identified records that have been modified. In either instance the question
that matters is the same: **is this case clinically valid**, that is, could it
occur in practice, and is it internally consistent as a clinical picture.

**3.2** Before labeling a case, Contributor is asked to attest that the case is
clinically valid. That attestation is recorded against that specific case, with
the version of this Agreement in force at the time.

**3.3 THE POINT OF THE ATTESTATION.** Archangel relies on Contributor's
attestation. A case that Contributor attests is clinically valid is treated as
clinically valid, and is packaged and licensed on that basis. If Contributor
attests that a case is clinically valid when it is not, responsibility for that
attestation rests with Contributor and not with Archangel.

**3.4 REJECTING A CASE IS ALWAYS AVAILABLE AND IS NEVER PENALISED.** Contributor
may mark any case as not clinically valid instead of attesting to it. Doing so
sends the case to Archangel for review, produces no labels, and moves
Contributor to the next case. Rejecting cases is expected, is the correct
response to a case that is wrong, and does not count against Contributor's
standing, tier, or pay. Contributor is never required to attest to a case they
are not willing to attest to.

**3.5** Where Archangel determines, after review, that an attestation of
clinical validity was false, the case that attestation covered is not paid. Any
payment already settled for that case is not reversed. Section 4 governs how
that determination is made and what Contributor is told.

---

## 4. Pay, and the quality standard pay is tied to

**4.1 WORK IS PAID WHEN IT MEETS THE RUBRIC.** Contributor understands that each
case is reviewed, and that work which does not follow the rubric, or is rushed
or incomplete, may be paid at a reduced rate or, if it is unusable, not paid at
all.

**4.2** A reduction is decided by a person, never automatically, and it is never
below 60% of the posted rate for a case Contributor completed.

**4.3** If a case is reduced or not paid, Contributor will be told which case and
why, and may ask for it to be looked at again.

**4.4** The rate in force when a case is completed is the rate that case is worth.
A later change to the posted rate applies to later work and never restates work
already done.

**4.5** Section 3.5 is the one circumstance in which a completed case is not paid
for a reason other than the rubric. It is decided by a person on the same terms
as 4.2 and 4.3: Contributor is told which case and why, and may ask for it to be
looked at again.

---

## 5. Credentials

**5.1** Contributor consents to attaching their verified credential metadata to
the records they label, and to sharing that metadata with data buyers.

**5.2** Contributor attests that they are not currently subject to an active
disciplinary action by any state medical board, and that their licence is active
and unrestricted.

**5.3** Contributor will tell Archangel if either statement in 5.2 stops being
true.

---

## 6. Confidentiality

**6.1** Contributor will keep the cases, prompts, model outputs and other
physicians' work they see on the platform confidential, and will not reproduce
or republish them.

**6.2** This obligation survives the end of this Agreement.

---

## 7. Patient health information

**7.1** Contributor confirms they will not enter any patient health information
into Archangel.

**7.2** This applies to every free-text field on the platform, including notes,
rationales and rejection reasons.

---

## 8. Rights in the work

**8.1** Contributor assigns to Archangel, and grants Archangel a license under,
all rights in the labels, corrections, rankings and written judgments they
produce on the platform, so that they may be packaged and licensed as training
and evaluation data.

**8.2** This assignment covers the work product only. It does not cover
Contributor's underlying clinical knowledge, their credentials, or anything they
produce outside the platform.

---

## 9. General

**9.1 Term and termination.** Either Party may end this Agreement at any time on
notice. Ending it does not affect work already submitted, money already earned,
or the obligations in Sections 6, 7 and 8.

**9.2 No exclusivity.** Nothing here prevents Contributor from doing similar work
elsewhere.

**9.3 Taxes.** Contributor is responsible for their own taxes on amounts paid
under this Agreement. Archangel issues the tax documents the law requires it to
issue.

**9.4 Amendment by new version.** Archangel may issue a new version of this
Agreement. A new version does not take effect for Contributor until Contributor
has read and signed it, and Contributor is asked to do so before drawing their
next case. Work already in progress is not interrupted.

**9.5 Electronic signature.** The Parties agree that this Agreement may be
executed and delivered electronically, and that an electronic signature
constitutes an original signature for all purposes. Each Party consents to
conduct this transaction by electronic means under the Electronic Signatures in
Global and National Commerce Act, 15 U.S.C. § 7001 et seq., and the Uniform
Electronic Transactions Act as enacted in the governing jurisdiction. A record
of the signature, including the signer's typed name, the date and time of
signature in Coordinated Universal Time, the network address from which it was
made, and a cryptographic hash of the exact text signed, is retained by
Archangel and is available to Contributor in the Archangel portal and by
request.

---

## Signature

By signing below, Contributor represents that they have read this Agreement and
agree to it.

**Contributor: {{PHYSICIAN_NAME}}**

Signed by: {{SIGNER_NAME}}
Initials: {{SIGNED_INITIALS}}
Date (UTC): {{SIGNED_AT}}

**Archangel Health Inc.**

Countersigned on execution by an authorized officer of Archangel Health Inc.
