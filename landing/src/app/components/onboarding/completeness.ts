/* What is filled, what came from the CV, and what is still missing.
 *
 * The review screen renders every credential field at once, and the complaint
 * it answers is not that it is long. It is that nothing on it distinguishes:
 *
 *   - a field the CV filled in and nobody has checked,
 *   - a field the physician typed,
 *   - the two fields that actually stop them submitting,
 *   - and the twenty six that do not.
 *
 * All four looked identical, so the honest reading of the page was "everything
 * here is required and half of it is mysteriously already done".
 *
 * Kept PURE and free of React so the rules can be asserted directly, and so
 * Step5Credentials does not grow another two hundred lines of branching. It
 * imports only the erased `Credentials` type, so the dependency runs one way.
 */

import type { Credentials } from "./steps";

export type FieldStatus =
  /** The parse filled it and the physician has not touched it since. */
  | "fromCv"
  /** It has a value. */
  | "filled"
  /** Empty, and Submit is disabled until it is not. Exactly two, ever. */
  | "emptyRequired"
  /** Empty, does not block, and changes the outcome more than anything else. */
  | "emptyNeeded"
  /** Empty, worth having. */
  | "emptyRecommended"
  /** Empty, nice to have. Never counted, for the reason on `total` below. */
  | "emptyOptional";

/* THREE SECTIONS, NOT FIVE, and they are the three phase blocks the file
   already has. The review screen is those same blocks with `show()`
   short-circuited, so aligning the boxes to them means the sections are a
   wrapper around existing JSX rather than a re-ordering of thirty controls
   into new groups. Moving fields between boxes would be a much larger
   change to a form that is already the most load-bearing screen here. */
export type SectionId = "identity" | "training" | "focus";

export type FieldSummary = { key: string; label: string; status: FieldStatus };

export type SectionSummary = {
  id: SectionId;
  title: string;
  why: string;
  fields: FieldSummary[];
  filled: number;
  /** Counts required, needed and recommended fields ONLY.
   *
   *  Including the optional ones would print "9 of 28" on a form somebody has
   *  filled in properly, and a number that reads as failure when the answer is
   *  fine teaches people to stop reading the number. */
  total: number;
  /** Something in here is missing and matters. Drives the pink dot and, more
   *  importantly, forces the section open. */
  hasNeeded: boolean;
  /** Something in here came from the CV and has not been confirmed. A
   *  collapsed box must never hide a value we guessed on their behalf. */
  hasUnconfirmedCv: boolean;
};

type Ctx = {
  isUS: boolean;
  /** False when `countryOfLicensure` is still empty. See `identifierField`. */
  countrySet: boolean;
  registryName: string;
};

/* The repeatable groups (board certifications, fellowship, residency) always
   hold at least one row, and that row starts EMPTY. Counting length alone
   reported an untouched form as complete, which is the one thing a
   completeness summary may never do. A row counts when something in it does. */
const rowHasContent = (row: unknown): boolean =>
  !!row && typeof row === "object"
    ? Object.values(row as Record<string, unknown>).some(
        (v) => typeof v === "string" ? v.trim().length > 0 : !!v)
    : !!row;

const has = (v: unknown): boolean =>
  Array.isArray(v)
    ? v.some(rowHasContent)
    : typeof v === "string" ? v.trim().length > 0 : v != null;

function statusFor(
  key: string,
  value: unknown,
  weight: "required" | "needed" | "recommended" | "optional",
  autofilled: Set<string>,
): FieldStatus {
  if (has(value)) return autofilled.has(key) ? "fromCv" : "filled";
  if (weight === "required") return "emptyRequired";
  if (weight === "needed") return "emptyNeeded";
  if (weight === "recommended") return "emptyRecommended";
  return "emptyOptional";
}

/** Which identifier this physician is even being asked for, and how hard.
 *
 *  THE EDGE CASE THIS EXISTS FOR: the form treats an unset country as US, so a
 *  consultant in Riyadh who has not yet picked one would see a red marker on an
 *  NPI field they can never hold. Until the country is answered the identifier
 *  question is unanswerable, so the marker moves to the country instead.
 */
export function identifierField(c: Credentials, ctx: Ctx): {
  key: "npi" | "registrationNumber" | "countryOfLicensure";
  weight: "needed" | "recommended";
} {
  if (!ctx.countrySet) return { key: "countryOfLicensure", weight: "needed" };
  return { key: ctx.isUS ? "npi" : "registrationNumber", weight: "needed" };
}

export function reviewSections(
  c: Credentials,
  autofilled: Set<string>,
  ctx: Ctx,
): SectionSummary[] {
  const ident = identifierField(c, ctx);
  const w = (key: string): "required" | "needed" | "recommended" | "optional" => {
    if (key === "fullLegalName" || key === "primarySpecialty") return "required";
    if (key === ident.key) return "needed";
    if (["degree", "boardCertifications", "phone", "licenseNumber",
         "residency", "cvFilename"].indexOf(key) !== -1) return "recommended";
    return "optional";
  };
  const f = (key: string, label: string, value: unknown): FieldSummary =>
    ({ key, label, status: statusFor(key, value, w(key), autofilled) });

  const build = (
    id: SectionId, title: string, why: string, fields: FieldSummary[],
  ): SectionSummary => {
    const counted = fields.filter((x) => w(x.key) !== "optional");
    return {
      id, title, why, fields,
      filled: counted.filter((x) => x.status === "fromCv" || x.status === "filled").length,
      total: counted.length,
      hasNeeded: fields.some((x) => x.status === "emptyRequired" || x.status === "emptyNeeded"),
      hasUnconfirmedCv: fields.some((x) => x.status === "fromCv"),
    };
  };

  const identityFields: FieldSummary[] = [
    f("countryOfPractice", "Where you practise", c.countryOfPractice),
    f("countryOfLicensure", "Where you are licensed", c.countryOfLicensure),
    ctx.isUS
      ? f("npi", "NPI number", c.npi)
      : f("registrationNumber", ctx.registryName || "Registration number", c.registrationNumber),
    f("degree", "Degree", c.degree),
    f("phone", "Your mobile number", c.phone),
    f("licenseNumber", "Licence number", c.licenseNumber),
    f("licenseState", "Licence state", c.licenseState),
  ];

  return [
    build("identity", "Who you are, and how we verify you",
      ctx.isUS
        ? "Your name and specialty are all we need to accept this. The NPI is "
          + "what lets us check you automatically: with it, verification usually "
          + "finishes the same day, and without it a person reads your file by hand."
        : "Your name and specialty are all we need to accept this. Your "
          + "registration number is what lets us check you against your registry "
          + "rather than by hand.",
      [
        f("fullLegalName", "Full legal name", c.fullLegalName),
        f("primarySpecialty", "Primary specialty", c.primarySpecialty),
        ...identityFields,
      ]),
    build("training", "Your training and practice",
      "Board certification and fellowship are how specialist casework reaches "
      + "you rather than a generalist. Whether you are practising now matters "
      + "too, and part time counts.",
      [
        f("boardCertifications", "Board certifications", c.boardCertifications),
        f("fellowship", "Fellowship", c.fellowship),
        f("residency", "Residency", c.residency),
        f("practiceStatus", "Current practice status", c.practiceStatus),
      ]),
    build("focus", "What decides the work we send you",
      "All optional. Specialist and multilingual work pays materially more "
      + "than general review, and we can only route it to you if we know it "
      + "about you.",
      [
        f("linkedinUrl", "LinkedIn", c.linkedinUrl),
        f("healthSystem", "Health system or practice", c.healthSystem),
        f("subspecialties", "Subspecialty and focus areas", c.subspecialties),
        f("structuredReviewExperience", "Structured review experience",
          c.structuredReviewExperience),
        f("practiceSettings", "Practice setting", c.practiceSettings),
        f("practiceCity", "City you practise in", c.practiceCity),
        f("languages", "Languages", c.languages),
      ]),
  ];
}

/** The three sentences at the top of the review page. */
export function checklistRows(
  sections: SectionSummary[],
  c: Credentials,
  cvParsedOk: boolean,
): { tone: "ok" | "cv" | "gap"; text: string }[] {
  const rows: { tone: "ok" | "cv" | "gap"; text: string }[] = [];

  const missingRequired = sections
    .flatMap((s) => s.fields)
    .filter((x) => x.status === "emptyRequired")
    .map((x) => x.label.toLowerCase());
  rows.push(missingRequired.length
    ? { tone: "gap", text: "One thing left before you can submit: your "
        + missingRequired.join(" and ") + "." }
    : { tone: "ok", text: "Ready to submit. Your name and specialty are in, and "
        + "that is all we need." });

  const fromCv = sections.flatMap((s) => s.fields).filter((x) => x.status === "fromCv");
  if (cvParsedOk && fromCv.length) {
    rows.push({ tone: "cv", text: fromCv.length
      + " fields came from your CV. They are our reading, not yours, until you check them." });
  }

  const gaps = sections
    .flatMap((s) => s.fields)
    .filter((x) => x.status === "emptyNeeded" || x.status === "emptyRecommended")
    .map((x) => x.label);
  if (gaps.length) {
    rows.push({ tone: "gap", text: gaps.length
      + " still missing: " + gaps.join(", ")
      + ". None of it blocks you, all of it speeds up your review." });
  }
  return rows;
}
