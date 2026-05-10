"""X12 271 envelope parser.

PRD §7.1 shape. Tokenizes by ISA-declared delimiters, walks segments, builds an AST
focused on the fields the 6 TEAM checks care about: EB, DTP, REF, NM1, MSG, AAA, MSP.

Raises:
  InvalidX12Error: not a recognizable X12 envelope.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


class InvalidX12Error(ValueError):
    """Raised when the bytes don't start with a valid ISA envelope."""


@dataclass
class Benefit:
    eb01: str = ""  # status: 1=Active, 6=Inactive, L=Primary, V=Non-Primary, ...
    eb03: str = ""  # service type: MA=Part A, MB=Part B, 30=Health Plan
    plan_begin: Optional[str] = None  # DTP*346
    plan_end: Optional[str] = None  # DTP*347
    payer_name: str = ""  # NM1*PR
    contract_id: str = ""  # REF*18
    industry_codes: List[str] = field(default_factory=list)  # III
    messages: List[str] = field(default_factory=list)  # MSG


@dataclass
class AAAError:
    """Rejection or 'unable to respond' marker. PRD §11.1."""

    reject_reason: str = ""  # AAA*03
    follow_up: str = ""  # AAA*04


@dataclass
class X12_271_AST:
    subscriber: dict = field(default_factory=dict)
    benefits: List[Benefit] = field(default_factory=list)
    msp: dict = field(default_factory=dict)
    messages: List[str] = field(default_factory=list)
    errors: List[AAAError] = field(default_factory=list)
    raw: str = ""

    def to_dict(self) -> dict:
        return {
            "subscriber": self.subscriber,
            "benefits": [vars(b) for b in self.benefits],
            "msp": self.msp,
            "messages": self.messages,
            "errors": [vars(e) for e in self.errors],
        }


def _parse_subscriber(fields_: List[str]) -> dict:
    # NM1*IL*1*LAST*FIRST*M***MI*123456789A
    last = fields_[3] if len(fields_) > 3 else ""
    first = fields_[4] if len(fields_) > 4 else ""
    middle = fields_[5] if len(fields_) > 5 else ""
    id_qual = fields_[8] if len(fields_) > 8 else ""
    id_code = fields_[9] if len(fields_) > 9 else ""
    return {
        "last_name": last.strip(),
        "first_name": first.strip(),
        "middle_name": middle.strip(),
        "id_qualifier": id_qual.strip(),
        "id_code": id_code.strip(),  # MBI when id_qualifier == "MI"
    }


def _parse_aaa(fields_: List[str]) -> AAAError:
    reason = fields_[3] if len(fields_) > 3 else ""
    follow = fields_[4] if len(fields_) > 4 else ""
    return AAAError(reject_reason=reason.strip(), follow_up=follow.strip())


def parse_x12_271(raw: str) -> X12_271_AST:
    """Parse an X12 271 envelope into an AST.

    Delimiters per ISA header:
      - element_delim  = raw[3]    (e.g. '*')
      - segment_delim  = raw[105]  (e.g. '~')
    """
    if not raw or "ISA" not in raw[:106]:
        raise InvalidX12Error("Not a valid X12 271 envelope (missing ISA header)")
    # Header ISA is fixed-length: position 105 is the segment terminator
    segment_delim = raw[105]
    element_delim = raw[3]
    segments = [s.strip() for s in raw.split(segment_delim) if s.strip()]

    ast = X12_271_AST(raw=raw)
    current: Optional[Benefit] = None

    for seg in segments:
        fields_ = seg.split(element_delim)
        tag = fields_[0].strip()

        if tag == "NM1" and len(fields_) > 1:
            role = fields_[1].strip()
            if role == "IL":  # Insured / Subscriber
                ast.subscriber = _parse_subscriber(fields_)
            elif role == "PR" and current is not None:
                # Payer on the current benefit
                if len(fields_) > 3:
                    current.payer_name = fields_[3].strip()
        elif tag == "EB":
            current = Benefit(
                eb01=fields_[1].strip() if len(fields_) > 1 else "",
                eb03=fields_[3].strip() if len(fields_) > 3 else "",
            )
            ast.benefits.append(current)
        elif tag == "DTP" and current is not None and len(fields_) > 3:
            qual = fields_[1].strip()
            val = fields_[3].strip()
            if qual == "346":
                current.plan_begin = val
            elif qual == "347":
                current.plan_end = val
        elif tag == "REF" and current is not None and len(fields_) > 2:
            if fields_[1].strip() == "18":  # Plan/Contract Number
                current.contract_id = fields_[2].strip()
        elif tag == "III" and current is not None and len(fields_) > 2:
            current.industry_codes.append(fields_[2].strip())
        elif tag == "MSG" and len(fields_) > 1:
            msg_text = fields_[1].strip()
            if current is not None:
                current.messages.append(msg_text)
            else:
                ast.messages.append(msg_text)
        elif tag == "AAA":
            ast.errors.append(_parse_aaa(fields_))
        # MSP indicator — some payers emit a custom segment; most express
        # primary/secondary via EB01 status codes, which the extractor handles.

    return ast


def format_for_llm(ast: X12_271_AST) -> str:
    """Render the AST into a compact, LLM-friendly block (used by extract.py)."""
    lines: list[str] = ["=== X12 271 PARSED ==="]
    sub = ast.subscriber or {}
    if sub:
        mbi = sub.get("id_code", "") if sub.get("id_qualifier") == "MI" else ""
        lines.append(
            f"Subscriber: {sub.get('first_name','')} {sub.get('last_name','')}"
            f"  MBI={mbi or '(not MI-qualified)'}"
        )
    if ast.errors:
        lines.append("AAA ERRORS (payer couldn't fulfill):")
        for e in ast.errors:
            lines.append(f"  - reject_reason={e.reject_reason} follow_up={e.follow_up}")
    lines.append(f"BENEFITS (n={len(ast.benefits)}):")
    for i, b in enumerate(ast.benefits, 1):
        lines.append(
            f"  #{i} EB01={b.eb01} EB03={b.eb03} "
            f"begin={b.plan_begin or '-'} end={b.plan_end or '-'} "
            f"payer={b.payer_name or '-'} contract={b.contract_id or '-'}"
        )
        for code in b.industry_codes:
            lines.append(f"      III={code}")
        for m in b.messages:
            lines.append(f"      MSG={m}")
    if ast.messages:
        lines.append("TOP-LEVEL MESSAGES:")
        for m in ast.messages:
            lines.append(f"  - {m}")
    return "\n".join(lines)
