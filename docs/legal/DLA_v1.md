# DATA LICENSING AGREEMENT

**Version v1**

<!--
  THIS FILE IS THE SOURCE OF THE AGREEMENT THAT IS RENDERED IN THE PORTAL AND
  SIGNED. It is not documentation about the agreement.

  Three rules, all load-bearing:

  1. EDITING THIS FILE CHANGES ITS SHA256, AND THE SHA256 IS WHAT WAS SIGNED.
     Every signature row records the hash of the exact text that was on the
     signer's screen. A typo fix here does not retroactively change what anyone
     agreed to -- it produces a document that no longer matches the signatures
     already taken, which is exactly what the hash exists to reveal. Fix a typo
     by shipping v2 (a new file, docs/legal/DLA_v2.md, and a bump of
     asclepius/dla.CURRENT_VERSION); do not edit a version anyone has signed.

  2. The {{PLACEHOLDERS}} below are substituted per organization at render time
     by asclepius/dla.py. Adding one means teaching that module about it; an
     unknown placeholder is left in the text on purpose, so it shows up on
     screen rather than silently rendering as an empty space in a contract.

  3. Counsel reviews before the first real signature. This is the engineering
     scaffolding for a document a lawyer owns, and the comment stays here until
     that review has happened.

  HTML comments are stripped before rendering, so nothing in this block ever
  reaches a signer.
-->

This Data Licensing Agreement (this "**Agreement**") is entered into as of
{{EFFECTIVE_DATE}} (the "**Effective Date**") by and between:

**{{LICENSOR_NAME}}**, a health care organization (together with its affiliates,
"**Licensor**"), and

**Archangel Health Inc.**, a Delaware corporation ("**Archangel**").

Licensor and Archangel are each a "**Party**" and together the "**Parties**".

---

## 1. Parties and Recitals

**1.1** Licensor holds clinical records generated in the course of providing
health care and has the ability to prepare de-identified extracts of those
records.

**1.2** Archangel builds and operates a platform on which licensed physicians
annotate, review, evaluate and adjudicate de-identified clinical material, and
licenses the resulting material and the underlying de-identified data to third
parties, including developers and evaluators of artificial intelligence systems.

**1.3** Licensor wishes to license De-identified Data to Archangel, and Archangel
wishes to receive it, on the terms below.

**1.4** The Parties intend this Agreement to be a binding contract. Nothing in
the recitals creates an obligation independent of the operative sections that
follow.

---

## 2. Definitions

**2.1 "De-identified Data"** means health information from which identifiers
have been removed such that the information is not individually identifiable
health information under 45 C.F.R. § 164.514(a), determined by either method
permitted by 45 C.F.R. § 164.514(b): the removal of the eighteen identifier
categories enumerated in § 164.514(b)(2) ("**Safe Harbor**"), or a documented
determination by a person with appropriate knowledge and experience applying
generally accepted statistical and scientific principles ("**Expert
Determination**"). Dates that are more specific than year are date-shifted or
generalized as required by Safe Harbor, and ages over 89 are aggregated.

**2.2 "Derived Works"** means any material Archangel or its personnel create
from De-identified Data, including annotation tasks, labels, rationales, expert
review and adjudication records, evaluation sets, benchmarks, rubrics, scoring
data, synthetic or transformed variants, and datasets assembled for the training
or evaluation of machine learning systems.

**2.3 "Brokering"** means the licensing, sublicensing, sale or other
distribution of De-identified Data or Derived Works to a third party.

**2.4 "Task Creation"** means the internal use of De-identified Data to produce
Derived Works on the Archangel platform, including presenting material to
physicians for annotation, review, evaluation and adjudication.

**2.5 "Recipient"** means a third party to whom Archangel discloses
De-identified Data or Derived Works under Section 3.

**2.6 "Re-identify"** means to use information, alone or in combination with
other information, to identify an individual who is a subject of De-identified
Data, or to contact such an individual.

**2.7 "Protected Health Information"** or "**PHI**" has the meaning given in
45 C.F.R. § 160.103.

---

## 3. License Grant

**3.1 Grant.** Subject to the terms of this Agreement, Licensor grants Archangel
a non-exclusive, worldwide, royalty-bearing (as provided in Section 6), and,
during the Term, irrevocable license to receive, store, reproduce, process,
adapt and use De-identified Data for both of the following purposes:

  **(a) Task Creation and Derived Works.** To create, reproduce, modify and use
  Derived Works; and

  **(b) Brokering.** To license, sublicense and distribute De-identified Data
  and Derived Works to Recipients.

**3.2 Both purposes are granted.** Both purposes in Section 3.1 are granted
together. **The allocation of any given record, file or extract between Task
Creation and Brokering is at Archangel's sole discretion**, and Archangel is not
required to notify Licensor of, or seek consent for, the allocation of any
particular record. Licensor acknowledges that it will not receive a
record-by-record accounting of which purpose any item of De-identified Data was
applied to, and that this allocation may change over time.

**3.3 Derived Works ownership.** As between the Parties, Archangel owns all
right, title and interest in Derived Works, including the contributions of
physicians and reviewers engaged by Archangel. Licensor retains all right, title
and interest in the De-identified Data it provides, subject to the license
granted in Section 3.1.

**3.4 Downstream terms.** Archangel shall not disclose De-identified Data or
Derived Works to any Recipient except under a written agreement that binds that
Recipient to the no-re-identification obligation in Section 5.1 and requires the
Recipient to impose the same obligation on any party to whom it onward
discloses.

**3.5 No license to PHI.** Nothing in this Agreement licenses, and Archangel
does not seek, PHI. Where the Parties agree that identified data must be
transferred, Section 4.2 governs and no transfer occurs until the addendum
described there is executed.

**3.6 Feedback.** Licensor may provide suggestions about the Archangel platform.
Archangel may use such suggestions without restriction or obligation.

---

## 4. Licensor Representations and Covenants

Licensor represents, warrants and covenants that:

**4.1 Authority.** Licensor has full right, power and authority to enter into
this Agreement and to grant the license in Section 3.1, and the individual
signing this Agreement is authorized to bind Licensor.

**4.2 De-identification.** All data Licensor transfers to Archangel will be
De-identified Data at the time of transfer, de-identified within Licensor's own
environment and before it leaves that environment, together with any date
shifting required by Section 2.1. If the Parties agree that Archangel is to
receive information that is not De-identified Data, they will first execute a
business associate agreement or other addendum meeting the requirements of 45
C.F.R. §§ 164.502(e) and 164.504(e), and no such transfer will occur before that
addendum is executed by both Parties.

**4.3 No conflicting restriction.** To Licensor's knowledge after reasonable
inquiry, no patient consent or authorization term, research protocol,
institutional review board condition, data use agreement, license, or other
agreement binding Licensor prohibits the commercial use of the De-identified
Data or its transfer to third parties as contemplated by Section 3.1. Licensor
will promptly notify Archangel if it becomes aware that any such restriction
applies to data already transferred.

**4.4 Method of de-identification.** On Archangel's reasonable request, Licensor
will identify which method under Section 2.1 was applied to a given transfer
and, where Expert Determination was used, will provide the determination
documentation or a summary of it.

**4.5 No malicious content.** Licensor will use commercially reasonable efforts
to ensure that files transferred to Archangel are free of malware.

**4.6 Correction.** If Licensor determines that data it transferred was not in
fact De-identified Data, it will notify Archangel without unreasonable delay and
in any event within five (5) business days of that determination, and the
Parties will cooperate under Section 5.4.

---

## 5. Archangel Covenants

**5.1 No re-identification.** Archangel will not Re-identify, or attempt to
Re-identify, any individual who is a subject of De-identified Data, and will not
permit any Recipient or other party acting on its behalf to do so. Archangel
will impose this obligation contractually on every Recipient, with flow-down as
required by Section 3.4.

**5.2 Security program.** Archangel will maintain an information security
program with administrative, physical and technical safeguards appropriate to
the sensitivity of the De-identified Data, including encryption of data in
transit and at rest, access control on a least-privilege basis, logging of
access to licensed data, and periodic review of that program.

**5.3 Personnel.** Archangel will bind its personnel and its contracted
physicians to confidentiality obligations and to the no-re-identification
obligation in Section 5.1.

**5.4 Notice of incidents.** Archangel will notify Licensor without unreasonable
delay, and in any event within five (5) business days, after Archangel becomes
aware of (a) any unauthorized acquisition, access, use or disclosure of
De-identified Data received from Licensor, or (b) any actual or attempted
Re-identification of an individual whose data Licensor provided. The notice will
describe what is known, and Archangel will provide updates as the facts develop.

**5.5 No representation about outputs.** Archangel makes no representation that
any Derived Work, model, benchmark or evaluation is fit for clinical use, and
nothing in this Agreement constitutes medical advice or a clinical service to
Licensor.

**5.6 Records.** Archangel will retain records sufficient to identify the
De-identified Data received from Licensor and the compensation calculated under
Section 6.

---

## 6. Compensation

**6.1 Schedule A.** Compensation payable to Licensor for the license granted in
Section 3.1 is set out in Schedule A to this Agreement or in a revenue-share
addendum executed by both Parties. Schedule A may be amended by a writing signed
by both Parties without amending the remainder of this Agreement.

**6.2 Where no Schedule A exists.** If no Schedule A or revenue-share addendum
has been executed as of a transfer of data, that transfer is made without
monetary consideration, and neither Party owes the other any payment for it.
This Section 6.2 does not limit either Party's right to negotiate a Schedule A
covering later transfers, and does not convert any transfer into a sale.

**6.3 Statements and payment.** Where a Schedule A is in effect, Archangel will
provide Licensor a statement of amounts due for each period specified in it, and
will pay undisputed amounts within thirty (30) days after the end of that
period. Statements and payment records are visible to Licensor in the Archangel
portal.

**6.4 Taxes.** Each Party is responsible for its own taxes arising from this
Agreement. Archangel does not hold Licensor's bank account details or tax
identifiers; payment instructions are exchanged through Licensor's designated
contract contact.

**6.5 Disputed amounts.** Licensor may dispute a statement within sixty (60)
days of receiving it. The Parties will resolve disputed amounts in good faith,
and an unresolved dispute over an amount does not suspend either Party's other
obligations.

---

## 7. Term and Termination

**7.1 Term.** This Agreement begins on the Effective Date and continues for two
(2) years, and then renews automatically for successive one (1) year periods
unless either Party gives written notice of non-renewal at least sixty (60) days
before the end of the then-current period (the "**Term**").

**7.2 Termination for cause.** Either Party may terminate this Agreement if the
other Party materially breaches it and fails to cure the breach within thirty
(30) days after written notice describing it. Archangel may terminate
immediately on notice if it reasonably determines that continued receipt of data
from Licensor would violate applicable law.

**7.3 Termination for convenience.** Either Party may terminate this Agreement
for any reason on sixty (60) days' written notice.

**7.4 Effect of termination.** On termination or expiration:

  **(a)** Licensor will make no further transfers of De-identified Data, and
  Archangel will make no further disclosures of De-identified Data to new
  Recipients;

  **(b)** licenses previously granted to Recipients survive according to their
  terms, and Derived Works already created survive and may continue to be used
  and licensed by Archangel, because they are already embedded in third-party
  systems and evaluations and cannot be recalled without destroying the value of
  work performed before termination;

  **(c)** Archangel may retain De-identified Data to the extent necessary to
  support Derived Works described in clause (b), to meet its legal and audit
  obligations, and in routine backups, and the obligations in Section 5 continue
  to apply to any data so retained; and

  **(d)** amounts accrued and unpaid as of the effective date of termination
  remain payable.

**7.5 Survival.** Sections 2, 3.3, 3.4, 5.1, 5.2, 5.4, 6.3 (as to accrued
amounts), 7.4, 8, 9 and any Schedule A payment obligations survive termination.

---

## 8. Liability and Indemnity

**8.1 Disclaimer.** Except as expressly stated in this Agreement, each Party
disclaims all implied warranties, including merchantability, fitness for a
particular purpose and non-infringement.

**8.2 Cap.** Except for the Excluded Claims in Section 8.4, each Party's total
liability arising out of or relating to this Agreement will not exceed the
greater of (a) the total amounts paid or payable by Archangel to Licensor under
this Agreement in the twelve (12) months preceding the event giving rise to the
claim, and (b) fifty thousand United States dollars (US$50,000).

**8.3 No consequential damages.** Except for the Excluded Claims, neither Party
is liable for indirect, incidental, special, consequential, exemplary or
punitive damages, or for lost profits, revenue or data, even if advised of the
possibility.

**8.4 Excluded Claims.** The limitations in Sections 8.2 and 8.3 do not apply to
(a) either Party's indemnity obligations under Section 8.5, (b) Archangel's
breach of Section 5.1, (c) Licensor's breach of Section 4.1 or 4.2, or (d)
either Party's fraud or wilful misconduct.

**8.5 Indemnities.**

  **(a)** Licensor will defend and indemnify Archangel against third-party
  claims arising from Licensor's breach of Section 4.1 (authority), Section 4.2
  (de-identification) or Section 4.3 (no conflicting restriction), including any
  claim that data transferred to Archangel was not De-identified Data.

  **(b)** Archangel will defend and indemnify Licensor against third-party
  claims arising from Archangel's breach of Section 5.1 (no re-identification)
  or from Archangel's use or distribution of De-identified Data or Derived Works
  in a manner not permitted by this Agreement.

  **(c)** The indemnified Party will give prompt notice of the claim, allow the
  indemnifying Party to control the defence, and cooperate reasonably. No
  settlement that admits liability or imposes an obligation on the indemnified
  Party may be made without that Party's consent.

---

## 9. General

**9.1 Governing law and venue.** This Agreement is governed by the laws of the
State of Delaware, without regard to its conflict-of-laws rules. The Parties
submit to the exclusive jurisdiction of the state and federal courts located in
Delaware.

**9.2 Notices.** Notices under this Agreement are effective when sent to the
email address each Party has designated in the Archangel portal, with a
confirming copy for termination notices sent by a nationally recognized courier.
Each Party is responsible for keeping its designated address current.

**9.3 Assignment.** Neither Party may assign this Agreement without the other's
written consent, except to a successor in connection with a merger, acquisition
or sale of substantially all assets, on written notice.

**9.4 Independent contractors.** The Parties are independent contractors. This
Agreement creates no partnership, joint venture, agency or employment
relationship.

**9.5 Publicity.** Neither Party will use the other's name or marks in publicity
without prior written consent, except that Archangel may state internally and to
prospective Recipients that it licenses data from health care organizations
without naming Licensor.

**9.6 Entire agreement.** This Agreement, together with any Schedule A and any
executed addendum, is the entire agreement between the Parties on its subject
matter and supersedes all prior discussions and agreements on that subject
matter. Any conflicting or additional terms in a purchase order or vendor form
are of no effect.

**9.7 Amendment and waiver.** This Agreement may be amended only by a writing
signed by both Parties. A failure to enforce a provision is not a waiver of it.

**9.8 Severability.** If a provision is held unenforceable, it is modified to
the minimum extent necessary to make it enforceable, and the remainder of the
Agreement continues in effect.

**9.9 Electronic signature.** The Parties agree that this Agreement may be
executed and delivered electronically, and that an electronic signature
constitutes an original signature for all purposes. Each Party consents to
conduct this transaction by electronic means under the Electronic Signatures in
Global and National Commerce Act, 15 U.S.C. § 7001 et seq., and the Uniform
Electronic Transactions Act as enacted in the governing jurisdiction. Neither
Party will contest the validity, admissibility or enforceability of this
Agreement on the ground that it was signed electronically. A record of the
signature, including the signer's typed name and title, the date and time of
signature in Coordinated Universal Time, the network address from which it was
made, and a cryptographic hash of the exact text signed, is retained by
Archangel and is available to Licensor in the Archangel portal and by request.

**9.10 Counterparts.** This Agreement may be executed in counterparts, each of
which is an original and all of which together constitute one instrument.

---

## Schedule A — Compensation

*No Schedule A is in effect as of the Effective Date. Section 6.2 governs until
the Parties execute one. A Schedule A executed later is appended to this
Agreement by reference and does not require re-execution of the body of this
Agreement.*

---

## Signature

By signing below, the individual signing represents that they are authorized to
bind {{LICENSOR_NAME}} to this Agreement, and that they have read it.

**Licensor: {{LICENSOR_NAME}}**

Signed by: {{SIGNER_NAME}}
Title: {{SIGNER_TITLE}}
Date (UTC): {{SIGNED_AT}}

**Archangel Health Inc.**

Countersigned on execution by an authorized officer of Archangel Health Inc.
