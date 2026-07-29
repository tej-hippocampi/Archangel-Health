"""EHRState — a queryable case with the §8.4 temporal cutoff hard-enforced.

The environment does NOT hand the agent the whole chart (PRD §4, §13: "information
must be earned"). ``EHRState`` wraps a ClinicalCase dict and:

  * splits fields into an *observable-now* slice (returned at ``reset``) and an
    *earnable* remainder (returned by read tools only when asked);
  * on a REAL case, HARD-ENFORCES the temporal cutoff (PRD §8.4.2): any
    lab panel / note collected AFTER the decision point is *outcome/future* and a
    read tool can NEVER return it — enforced HERE, not by the case author
    (PRD §13);
  * re-checks every free-text observation for residual PHI at the tool boundary
    on a real case (PRD §8.4.5) via ``deid_verify``.

Works on plain dicts (that's what the codebase passes around — see ``cases.py``).
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from ..cases import as_dict

# Panels/notes at or before the decision point are visible/earnable; anything
# strictly after it is outcome-future and verifier-only.
_DEFAULT_DECISION_OFFSET = 0


class TemporalLeakError(RuntimeError):
    """Raised if code tries to surface post-decision-point data to the agent —
    a defense-in-depth backstop behind the zone partition (PRD §8.4.2)."""


class EHRState:
    def __init__(
        self,
        case: Dict[str, Any],
        *,
        decision_offset_days: Optional[int] = None,
        observable_panels: Optional[List[str]] = None,
        deid_recheck: bool = False,
        enforce_item_cutoff: Optional[bool] = None,
    ):
        # Deep-copy so each episode owns its state — a downstream consumer that
        # mutates a returned lab/note list can never corrupt the shared case dict
        # (compile stores one case object reused across every reset()).
        self.case: Dict[str, Any] = copy.deepcopy(as_dict(case) or {})
        self.case_source: str = self.case.get("case_source") or "synthetic"
        # On a REAL case the notes/studies temporal cutoff is enforced by their
        # ``collected_offset_days`` (populated by ``timeline`` from the note date):
        # a note/study AFTER the decision point is held out, and one with UNKNOWN
        # timing (None) is withheld fail-closed. Gold/synthetic have no future zone,
        # so unknown timing is visible. Default: enforce iff the case is real.
        self._enforce_item_cutoff = (
            (self.case_source == "real_deid") if enforce_item_cutoff is None else bool(enforce_item_cutoff)
        )
        # The decision point (PRD §8.4.1): the instant the agent is dropped in.
        # Everything is defined relative to this. For a synthetic/gold case the
        # whole authored chart is "then", so day 0 with no future zone.
        self.decision_offset_days = (
            _DEFAULT_DECISION_OFFSET if decision_offset_days is None else int(decision_offset_days)
        )
        # Which panels are visible at reset (observable-now). If unset, only the
        # earliest-offset panel(s) are visible and the rest must be earned.
        self._observable_panels = observable_panels
        # Re-check every returned free-text observation for residual PHI at the
        # tool boundary (PRD §8.4.5). Only meaningful on real de-identified data.
        self._deid_recheck = bool(deid_recheck) or self.case_source == "real_deid"
        # Track what the agent has already earned (for provenance / dense reward).
        self.revealed: Dict[str, Any] = {"panels": set(), "tools": []}

    # ─── Temporal zone gate (PRD §8.4.2 — the leakage-critical step) ──────────
    def _panel_offset(self, panel: Dict[str, Any]) -> int:
        try:
            return int(panel.get("collected_offset_days") or 0)
        except (TypeError, ValueError):
            return 0

    def _is_future(self, offset: int) -> bool:
        """A datum is outcome/future iff it was collected strictly AFTER the
        decision point. A read tool must never return it (PRD §8.4.2, §13)."""
        return offset > self.decision_offset_days

    def _visible_panels(self) -> List[Dict[str, Any]]:
        """All lab panels the agent is ALLOWED to see (now or earnable) — i.e.
        every panel at or before the decision point. Future panels are dropped
        here, at the state boundary, so no tool can leak the answer."""
        out = []
        for p in self.case.get("lab_panels") or []:
            if not self._is_future(self._panel_offset(p)):
                out.append(p)
        return out

    def _future_panels(self) -> List[Dict[str, Any]]:
        return [p for p in (self.case.get("lab_panels") or []) if self._is_future(self._panel_offset(p))]

    # ─── Note/study temporal gate (PRD §8.4.2 — narratives) ───────────────────
    def _item_offset(self, item: Dict[str, Any]) -> Optional[int]:
        v = item.get("collected_offset_days")
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def _item_visible(self, item: Dict[str, Any]) -> bool:
        """A note/study is visible unless it post-dates the decision point. On a real
        case, UNKNOWN timing (offset None) is withheld fail-closed — we never surface
        a narrative we cannot prove is pre-decision (that's where the outcome hides)."""
        off = self._item_offset(item)
        if off is None:
            return not self._enforce_item_cutoff
        return off <= self.decision_offset_days

    # ─── PHI re-check at the tool boundary (PRD §8.4.5) ───────────────────────
    def _scrub_guard(self, text: str) -> str:
        """Re-run the residual-identifier check on a free-text observation the
        moment it is returned (reuse ``deid_verify``). A flagged observation is
        withheld rather than shipped to the agent/trajectory/export."""
        if not self._deid_recheck or not text:
            return text
        try:
            from .. import deid_verify  # lazy — dev envs may lack heavy deps

            # Probe the real verifier entrypoints, in order. ``verify_deid`` is the
            # public API; ``residual_identifiers`` is the low-level scanner. If NO
            # verifier is found, fail CLOSED (withhold) — never silently pass raw
            # text through on a real case just because the API was refactored.
            findings = None
            probed = False
            for fn in ("residual_identifiers", "verify_deid", "scan_text", "find_phi"):
                f = getattr(deid_verify, fn, None)
                if callable(f):
                    probed = True
                    result = f(text)
                    # verify_deid returns a report; residual_identifiers a list.
                    if isinstance(result, dict):
                        findings = result.get("findings") or result.get("residual") or (
                            not result.get("ok", True) and result)
                    else:
                        findings = result
                    break
            if not probed:
                return "[withheld: no de-identification verifier available; failing closed]"
            if findings:
                return "[withheld: residual-identifier check flagged this note; case quarantined]"
        except Exception:
            # Fail-closed on a real case: if we cannot verify, do not leak.
            return "[withheld: de-identification re-check unavailable]"
        return text

    # ─── Observable-now slice (returned at reset, PRD §8.4.2) ──────────────────
    def observation_at_reset(self) -> Dict[str, Any]:
        """The opening observation: presenting complaint + demographics + vitals +
        problem list + the results available AT the decision point. Withheld
        fields (earnable labs, notes, studies, meds) are NOT included."""
        vis = self._visible_panels()
        if self._observable_panels is not None:
            now = [p for p in vis if p.get("panel") in set(self._observable_panels)]
        else:
            # Default: only the FIRST presenting panel is visible; deeper panels —
            # including same-offset panels — must be earned via get_labs (PRD §13,
            # "information must be earned"). Never dump the whole lab set at reset.
            now = [vis[0]] if vis else []
        for p in now:
            self.revealed["panels"].add(p.get("panel"))
        return {
            "demographics": self.case.get("demographics") or {},
            "vitals": self.case.get("vitals") or {},
            "problem_list": self.case.get("problem_list") or [],
            "labs_available_now": [self._panel_public(p) for p in now],
            "note": "Additional chart data (labs, notes, studies, medications) must be requested via tools.",
        }

    @staticmethod
    def _panel_public(panel: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "panel": panel.get("panel"),
            "collected_offset_days": panel.get("collected_offset_days", 0),
            "results": panel.get("results") or [],
        }

    # ─── Read-tool accessors (earnable zone — PRD §4 read tools) ──────────────
    def get_problem_list(self) -> List[Dict[str, Any]]:
        self.revealed["tools"].append("get_problem_list")
        return self.case.get("problem_list") or []

    def get_vitals(self) -> Dict[str, Any]:
        self.revealed["tools"].append("get_vitals")
        return self.case.get("vitals") or {}

    def get_medications(self) -> List[Dict[str, Any]]:
        self.revealed["tools"].append("get_medications")
        return self.case.get("medications") or []

    def get_labs(self, panel: Optional[str] = None) -> Dict[str, Any]:
        """Return a lab panel by name (or a directory of available panels). Only
        panels at/before the decision point are ever visible (temporal gate)."""
        panel = None if panel is None else str(panel)
        self.revealed["tools"].append(f"get_labs:{panel or '*'}")
        vis = self._visible_panels()
        if not panel:
            return {"available_panels": [p.get("panel") for p in vis]}
        want = panel.strip().lower()
        for p in vis:
            name = (p.get("panel") or "").strip().lower()
            if name == want or want in name:
                self.revealed["panels"].add(p.get("panel"))
                return self._panel_public(p)
        # Honest not_available (PRD §8.4.6) — missing data is itself a clinical state.
        return {"panel": panel, "results": [], "status": "not_available"}

    def get_notes(self, note_type: Optional[str] = None) -> List[Dict[str, Any]]:
        note_type = None if note_type is None else str(note_type)
        self.revealed["tools"].append(f"get_notes:{note_type or '*'}")
        # Temporal cutoff by note timing (real cases): a post-decision note — where
        # the outcome is written — is never returned; unknown-timing notes are
        # withheld fail-closed on real cases (PRD §8.4.2).
        withheld = 0
        out = []
        for n in self.case.get("notes") or []:
            nt = (n.get("note_type") or "").strip().lower()
            if note_type and nt != note_type.strip().lower():
                continue
            if not self._item_visible(n):
                withheld += 1
                continue
            out.append(
                {
                    "note_type": n.get("note_type"),
                    "author_role": n.get("author_role"),
                    "text": self._scrub_guard(n.get("text") or ""),
                }
            )
        if not out and withheld:
            return [{"status": "not_available",
                     "detail": "notes at/after the decision point are held out (temporal cutoff)"}]
        return out

    def get_studies(self, modality: Optional[str] = None) -> List[Dict[str, Any]]:
        modality = None if modality is None else str(modality)
        self.revealed["tools"].append(f"get_studies:{modality or '*'}")
        withheld = 0
        out = []
        for s in self.case.get("studies") or []:
            sm = (s.get("modality") or "").strip().lower()
            if modality and sm != modality.strip().lower():
                continue
            if not self._item_visible(s):
                withheld += 1
                continue
            out.append(
                {
                    "modality": s.get("modality"),
                    "label": s.get("label"),
                    "findings": self._scrub_guard(s.get("findings") or ""),
                    "measurements": s.get("measurements") or [],
                    "impression": s.get("impression"),
                }
            )
        if not out and withheld:
            return [{"status": "not_available",
                     "detail": "studies at/after the decision point are held out (temporal cutoff)"}]
        return out

    def get_timeline(self, window: Optional[int] = None) -> Dict[str, Any]:
        """Longitudinal read (PRD §4). Returns visible panels bucketed by
        ``collected_offset_days`` — NEVER future panels. ``window`` optionally
        limits to the last N days before the decision point."""
        self.revealed["tools"].append(f"get_timeline:{window or '*'}")
        vis = self._visible_panels()
        if window is not None:
            try:
                w = abs(int(window))
            except (TypeError, ValueError):
                w = None
            if w is not None:
                lo = self.decision_offset_days - w
                vis = [p for p in vis if self._panel_offset(p) >= lo]
        buckets: Dict[int, List[Dict[str, Any]]] = {}
        for p in vis:
            buckets.setdefault(self._panel_offset(p), []).append(self._panel_public(p))
        return {"decision_offset_days": self.decision_offset_days,
                "timepoints": [{"offset_days": k, "panels": v} for k, v in sorted(buckets.items())]}

    # ─── Verifier-only accessors (NEVER exposed to the agent) ─────────────────
    def held_out_outcome(self) -> Dict[str, Any]:
        """The outcome/future zone (PRD §8.4.2) — used only by ``verify.py`` for
        the outcome-verified reward. Calling this from a read tool is a bug; the
        tool layer never does."""
        return {
            "future_panels": [self._panel_public(p) for p in self._future_panels()],
            "ground_truth": self.case.get("ground_truth") or {},
        }

    def ground_truth(self) -> Dict[str, Any]:
        return self.case.get("ground_truth") or {}
