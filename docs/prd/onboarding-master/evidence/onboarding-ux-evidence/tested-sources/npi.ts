/**
 * NPI check-digit validation, client side (Onboarding v2 §2 screen 4).
 *
 * A direct port of `npi_checksum_ok` in `backend/asclepius/credentialing.py`.
 * Duplicating a rule across two languages is normally how they drift apart, and
 * this one is safe to duplicate for a specific reason: it is not a policy, it is
 * arithmetic. An NPI is ten digits whose check digit is the Luhn digit over the
 * card-issuer prefix "80840" plus the nine-digit base. That definition is fixed
 * by CMS and cannot change without every NPI in the country changing with it.
 *
 * It is also deliberately NOT a gate, here or on the server. A wrong check digit
 * shows an inline "double-check?" and the Submit button stays live — because the
 * commonest cause of a failing checksum is a typo the physician will fix in two
 * seconds, and the second commonest is that we are wrong about something. The
 * registry lookup decides; this only saves a round trip.
 */

/** Strip the separators people paste in with the number. */
export function cleanNpi(raw: string): string {
  return (raw || "").replace(/[\s\-.]/g, "");
}

/** True when `npi` is ten digits AND its check digit validates. */
export function npiChecksumOk(npi: string): boolean {
  if (!/^\d{10}$/.test(npi || "")) return false;
  const digits = "80840" + npi;
  let total = 0;
  for (let i = 0; i < digits.length; i += 1) {
    // Walk from the right: every second digit (0-indexed from the end) doubles.
    let d = Number(digits[digits.length - 1 - i]);
    if (i % 2 === 1) {
      d *= 2;
      if (d > 9) d -= 9;
    }
    total += d;
  }
  return total % 10 === 0;
}

/**
 * The inline note for an NPI field, or "" when there is nothing to say.
 *
 * Says nothing at all until ten digits are present: warning a physician about a
 * number they are three keystrokes into typing is noise, and noise is what makes
 * people stop reading the warnings that matter.
 */
export function npiWarning(raw: string): string {
  const value = cleanNpi(raw).trim();
  if (value.length < 10) return "";
  if (!/^\d{10}$/.test(value)) return "An NPI is exactly 10 digits.";
  if (!npiChecksumOk(value)) return "This doesn't look like a valid NPI, double-check?";
  return "";
}
