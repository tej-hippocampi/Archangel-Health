"""Conservative, deterministic clinical identity evidence from applicant CV text.

The decision is shared by the identifier and its display spelling. Evidence
metadata contains vocabulary values only, never resume excerpts or identities.
"""
from __future__ import annotations

import re

from asclepius.onboarding_specialties import _NAMES, match, normalize


_EXCLUDED_SECTION = re.compile(
    r"^(?:(?:selected|peer[- ]reviewed|professional|personal|academic)\s+)?"
    r"(?:references|referees|publications|bibliography|research(?:\s+(?:and|&))? publications|presentations)\b", re.I)
_SECTIONS = (
    ("identity", r"(?:professional |clinical )?(?:summary|profile)|personal details|contact information"),
    ("experience", r"(?:(?:professional|clinical|current|hospital|academic) )?"
     r"(?:experience|appointments|employment|practice)|employment history|work experience"),
    ("training", r"(?:(?:postgraduate|medical|professional) )?"
     r"(?:education|training|qualifications)|education (?:and|&) training|residency|fellowship"),
    ("board", r"(?:board )?certifications?|(?:licensure|licenses?) (?:and|&) board certifications?"),
    ("other", r"licensure|licenses?|awards|honors|honours|memberships|professional memberships|"
     r"clinical interests|research interests|research experience|objectives?"),
)
_DECLARED = re.compile(r"^(?:primary\s+)?specialt(?:y|ies)\s*:\s*(.+)$", re.I)
_LABELLED_CURRENT = re.compile(
    r"^current(?:\s+(?:clinical\s+)?(?:role|practice|position|specialty))?\s*:\s*(.+)$", re.I)
_UNASSERTED = re.compile(
    r"\b(?:not|no|never|pending|eligible|eligibility|scheduled|candidate|aspiring|aspire|"
    r"seeking|hoping|pursuing|future|intends?)\b|\b(?:interest(?:ed)? in|plans? to|applying for)\b", re.I)
_TRAINING = re.compile(r"\b(?:intern(?:ship)?|residen(?:t|cy)|fellow(?:ship)?)\b", re.I)
_ROLE = re.compile(
    r"\b(?:attending|consultant|physician|surgeon|specialist|practi[cs]ing|practitioner|"
    r"[a-z]*(?:ologist|iatrist)|intensivist|allergist|geneticist|internist|pediatrician|"
    r"paediatrician|anaesthetist)\b", re.I)
_CURRENT = re.compile(r"\b(?:present|current(?:ly)?)\b", re.I)
_HISTORICAL = re.compile(r"\b(?:former|previous|retired|past)\b", re.I)
_DATE_RANGE = re.compile(
    r"\b(?:19|20)\d{2}\s*[-–—]\s*(?:[A-Za-z]+\s+)?"
    r"(?P<end>(?:19|20)\d{2}|present|current)\b", re.I)
_BROAD_TRAINING = {"internal medicine", "pediatrics", "general surgery"}


def asserted_line(line: str) -> bool:
    return not _UNASSERTED.search(line)


def applicant_lines(text: str) -> list[tuple[str, str]]:
    """Skip third-party sections, with explicit section headings allowing re-entry."""
    section, excluded = "identity", False
    result = []
    for raw in text.splitlines():
        line = re.sub(r"^[•●▪*–-]\s+", "", raw.strip())
        if not line:
            if not excluded and result and result[-1][0]:
                result.append(("", section))
            continue
        heading = line.rstrip(":").strip()
        if _EXCLUDED_SECTION.match(heading):
            excluded = True
            continue
        recognized = next((name for name, pattern in _SECTIONS
                           if re.fullmatch(pattern, heading, re.I)), None)
        if recognized:
            section, excluded = recognized, False
            # Empty text marks the boundary so current-role context cannot
            # leak from one experience section into a later section.
            result.append(("", section))
        elif not excluded:
            result.append((line, section))
    return result


def candidate_fields(value: str) -> set[str]:
    """Use onboarding's vocabulary and nested-field rules, retaining conflicts."""
    unique = match(value)
    if unique:
        return {unique}
    text = " " + normalize(value) + " "
    spans = [(m.start(), m.end(), name) for name, aliases in _NAMES.items()
             for term in (name, *aliases)
             for m in re.finditer(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text)]
    return {
        ("pediatric " + name if not name.startswith("pediatric ")
         and re.search(r"(?:pediatric|paediatric)\s+$", text[:start]) else name)
        for start, end, name in spans if not any(
            left <= start and right >= end and (left < start or right > end)
            for left, right, _ in spans)
    }


def _unsupported_list_entry(value: str) -> bool:
    """A recognized member of an explicit list cannot hide an unknown member.

    Match complete vocabulary phrases before examining what remains. This
    preserves compounds such as Allergy & Immunology and Hematology/Oncology,
    whose separators are part of a single field. Do not discard descriptive
    suffixes: an unfamiliar second field must be confirmed by the physician.
    """
    if not re.search(r"[,;/&]|\b(?:and|or)\b", value, re.I):
        return False
    text = " " + normalize(value) + " "
    covered = [False] * len(text)
    for name, aliases in _NAMES.items():
        for term in (name, *aliases):
            for span in re.finditer(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text):
                start, end = span.span()
                # The shared matcher preserves pediatric fields even when
                # only their adult parent appears in the alias vocabulary.
                pediatric = re.search(r"\b(?:pediatric|paediatric)\s+$", text[:start])
                if pediatric:
                    start = pediatric.start()
                covered[start:end] = [True] * (end - start)
    remaining = "".join(" " if covered[index] else char for index, char in enumerate(text))
    return any(word not in {"and", "or"} for word in remaining.split())


def _role_line(line: str, section: str) -> bool:
    return bool(_TRAINING.search(line) or (
        section in {"identity", "experience"} and (
            _ROLE.search(line) or _LABELLED_CURRENT.match(line)
            or (section == "identity" and normalize(line) in _NAMES))))


def _dated_roles(lines: list[tuple[str, str]]) -> dict[int, str]:
    """Attach dates once, within an entry layout, never carry them forward.

    A paragraph can list date/title pairs or title/date pairs. Alternating
    date/role tokens establish which layout applies. Mixed layouts with
    no clear association leave roles undated; they cannot gain priority as a
    current post. Inline dates bind only their own role. Paragraph and section
    boundaries also prevent a date from lending authority to the next entry.
    """
    contexts = {}
    tokens: list[tuple[int, str, str]] = []

    def finish() -> None:
        if not tokens:
            return
        kinds = [kind for _, kind, _ in tokens]
        pattern = ["date", "role"] if kinds[0] == "date" else ["role", "date"]
        if kinds == pattern * (len(kinds) // 2):
            for offset in range(0, len(tokens), 2):
                pair = tokens[offset:offset + 2]
                position = next(index for index, kind, _ in pair if kind == "role")
                contexts[position] = next(state for _, kind, state in pair if kind == "date")
        tokens.clear()

    for index, (line, section) in enumerate(lines):
        if not line:
            finish()
            continue
        if (not asserted_line(line) or _DECLARED.match(line)
                or re.search(r"\bboard\b|\bcertif\w*\b", line, re.I)):
            continue
        role = _role_line(line, section)
        dated = _DATE_RANGE.search(line)
        state = ("current" if dated.group("end").lower() in {"present", "current"}
                 else "historical") if dated else ""
        if role and dated:
            finish()
            contexts[index] = state
        elif role:
            tokens.append((index, "role", ""))
        elif dated:
            tokens.append((index, "date", state))
    finish()
    return contexts


def specialty_evidence(text: str, certification_fields: list[str]) -> dict:
    """Resolve anchored evidence; equally strong conflicting fields remain empty."""
    evidence: set[tuple[int, str, str]] = set()
    unresolved: set[tuple[int, str]] = set()

    def add(value: str, source: str, rank: int) -> None:
        for field in candidate_fields(value):
            effective_rank = rank - 10 if source in {"board", "training"} and field in _BROAD_TRAINING else rank
            evidence.add((effective_rank, source, field))

    lines = applicant_lines(text)
    dates = _dated_roles(lines)
    for index, (line, section) in enumerate(lines):
        if not line:
            continue
        if not asserted_line(line):
            continue
        declared = _DECLARED.match(line)
        if declared:
            add(declared.group(1), "declared", 500)
            if not candidate_fields(declared.group(1)) or _unsupported_list_entry(declared.group(1)):
                unresolved.add((500, "declared"))
            continue
        labelled_current = _LABELLED_CURRENT.match(line)
        if labelled_current:
            add(labelled_current.group(1), "current_role", 400)
            if not candidate_fields(labelled_current.group(1)) or _unsupported_list_entry(labelled_current.group(1)):
                unresolved.add((400, "current_role"))
            continue
        current_entry = dates.get(index) == "current"
        historical_entry = dates.get(index) == "historical"
        training = _TRAINING.search(line)
        if training:
            # A current fellowship describes today's clinical field; an old
            # internship is weak evidence, even when it appears first.
            current_training = current_entry or bool(_CURRENT.search(line))
            add(line, "training", 350 if current_training and not _HISTORICAL.search(line) else 100)
            continue
        # Issuing boards and historical posts cannot masquerade as a current
        # professional title. Board evidence is supplied separately below.
        if re.search(r"\bboard\b|\bcertif\w*\b", line, re.I):
            continue
        if section not in {"identity", "experience"}:
            continue
        if _HISTORICAL.search(line):
            continue
        role = _ROLE.search(line)
        current = current_entry or bool(_CURRENT.search(line))
        bare_title = normalize(line) in _NAMES
        if historical_entry and not _CURRENT.search(line):
            continue
        if role or (section == "identity" and bare_title):
            add(line, "current_role" if current else "role", 400 if current else 300)

    for field in certification_fields:
        if asserted_line(field):
            add(field, "board", 200)

    candidates = [{"specialty": field, "source": source}
                  for rank, source, field in sorted(evidence, key=lambda e: (-e[0], e[2], e[1]))]
    priorities = [(rank, source) for rank, source, _ in evidence] + list(unresolved)
    if not priorities:
        return {"specialty": None, "specialty_source": "missing",
                "specialty_status": "missing", "specialty_candidates": candidates}
    top = max(rank for rank, _ in priorities)
    strongest = [entry for entry in evidence if entry[0] == top]
    fields = {entry[2] for entry in strongest}
    unknown = any(rank == top for rank, _ in unresolved)
    resolved = len(fields) == 1 and not unknown
    return {"specialty": next(iter(fields)) if resolved else None,
            "specialty_source": next(source for rank, source in priorities if rank == top),
            "specialty_status": "resolved" if resolved else "ambiguous" if fields else "missing",
            "specialty_candidates": candidates}
