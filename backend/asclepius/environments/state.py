"""EHRState — a queryable case with the §8.4 temporal cutoff hard-enforced.

The environment does NOT hand the agent the whole chart (PRD §4, §13: "information
must be earned"). ``EHRState`` wraps a ClinicalCase dict and:

  * splits fields into an *observable-now* slice (returned at ``reset``) and an
    *earnable* remainder (returned by read tools only when asked);
  * on a REAL case, HARD-ENFORCES the temporal cutoff (PRD §8.4.2) across the
    WHOLE chart — lab panels, notes, studies, problems, and medications — via ONE
    gate (:meth:`EHRState._item_visible`). Anything recorded after the decision
    point, or whose timing cannot be verified, is withheld. Enforced HERE, not by
    the case author (PRD §13);
  * re-checks every free-text observation for residual PHI at the tool boundary
    on a real case (PRD §8.4.5) via ``deid_verify``.

Why problems and medications matter as much as notes: a problem recorded after the
decision point IS the answer, and a drug started after it reveals the diagnosis
(tolvaptan → SIADH, rasburicase → tumor lysis, dialysis → the AKI outcome). Those
leaks land on the real, outcome-linked, premium-tier cases.

Works on plain dicts (that's what the codebase passes around — see ``cases.py``).
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from ..cases import as_dict

# Panels/notes at or before the decision point are visible/earnable; anything
# strictly after it is outcome-future and verifier-only.
_DEFAULT_DECISION_OFFSET = 0


class TemporalLeakError(RuntimeError):
    """Raised if code tries to surface post-decision-point data to the agent —
    a defense-in-depth backstop behind the zone partition (PRD §8.4.2)."""


def _has_word_prefix(name: str, want: str) -> bool:
    """True when ``want`` starts any WORD in ``name`` — so "renal" matches
    "renal function" but not "adrenal" (M4 tiered panel matching)."""
    return any(w.startswith(want) for w in re.split(r"[^a-z0-9]+", name) if w)


class EHRState:
    def __init__(
        self,
        case: Dict[str, Any],
        *,
        decision_offset_days: Optional[int] = None,
        observable_panels: Optional[List[str]] = None,
        deid_recheck: bool = False,
        enforce_item_cutoff: Optional[bool] = None,
        rng: Optional[Any] = None,
    ):
        # The episode RNG (from ``ClinicalEnv.reset(seed)``). Used ONLY for choices
        # the case/compiler leaves genuinely arbitrary — which panel presents at reset
        # when unspecified, and tie-breaking among equally-good get_labs matches — so
        # a seeded replay is bit-identical. Never used to alter what is *visible*.
        self._rng = rng
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
        # Both values are LISTS: this dict rides in ``step``/``reset`` ``info`` and is
        # persisted + exported, so a set would raise on json.dumps.
        self.revealed: Dict[str, Any] = {"panels": [], "tools": []}

    # ─── The temporal gate (PRD §8.4.2 — the leakage-critical step) ────────────
    # ONE code path for EVERY chart item (labs, notes, studies, problems, meds).
    # There is deliberately no separate panel path: a second implementation is how
    # a gate ends up failing open on one field and closed on another.
    def _item_offset(self, item: Dict[str, Any]) -> Optional[int]:
        """The item's recording day relative to the decision point, or ``None`` when
        it is absent/unparseable. ``None`` means UNKNOWN — never "day 0"; treating an
        unparseable offset as 0 would fail OPEN and show a post-decision item."""
        v = item.get("collected_offset_days")
        if v is None:
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    def _item_visible(self, item: Dict[str, Any]) -> bool:
        """Whether a chart item may be surfaced to the agent.

        Visible iff it was recorded at or before the decision point. UNKNOWN timing
        is withheld fail-CLOSED whenever the cutoff is enforced (real cases) — we
        never surface an item we cannot prove is pre-decision, because that is
        exactly where the outcome hides. Gold/synthetic cases have no future zone,
        so unknown timing is visible there."""
        off = self._item_offset(item)
        if off is None:
            return not self._enforce_item_cutoff
        return off <= self.decision_offset_days

    def _is_future(self, item: Dict[str, Any]) -> bool:
        """The verifier-side complement of :meth:`_item_visible` — strictly after the
        decision point (a KNOWN future item, i.e. the held-out outcome zone)."""
        off = self._item_offset(item)
        return off is not None and off > self.decision_offset_days

    def _visible_panels(self) -> List[Dict[str, Any]]:
        """Every lab panel the agent is ALLOWED to see (now or earnable). Future and
        unknown-timing panels are dropped HERE, at the state boundary, so no tool
        can leak the answer (PRD §8.4.2, §13)."""
        return [p for p in (self.case.get("lab_panels") or []) if self._item_visible(p)]

    def _future_panels(self) -> List[Dict[str, Any]]:
        return [p for p in (self.case.get("lab_panels") or []) if self._is_future(p)]

    def _gated(self, items: Optional[List[Dict[str, Any]]]) -> tuple:
        """Split a chart-item list into (visible, n_withheld) by the temporal gate."""
        visible, withheld = [], 0
        for it in items or []:
            if self._item_visible(it):
                visible.append(it)
            else:
                withheld += 1
        return visible, withheld

    @staticmethod
    def _withheld_marker(kind: str) -> Dict[str, Any]:
        return {"status": "not_available",
                "detail": f"{kind} recorded after the decision point (or with unverified timing) "
                          f"are held out — temporal cutoff"}

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
            # Default: only ONE presenting panel is visible; deeper panels — including
            # same-offset panels — must be earned via get_labs (PRD §13, "information
            # must be earned"). Never dump the whole lab set at reset. Which of the
            # equally-early panels presents is the arbitrary choice the seed governs.
            now = [self._pick(self._earliest_panels(vis))] if vis else []
        for p in now:
            self._mark_revealed(p.get("panel"))
        # The opening slice goes through the SAME gate + scrub as the read tools —
        # reset() is a read path too, and returning problem_list/vitals wholesale
        # here would leak past the gate on the very first observation.
        return {
            "demographics": self.case.get("demographics") or {},
            "vitals": self._public_vitals(),
            "problem_list": self._public_problems(),
            "labs_available_now": [self._panel_public(p) for p in now],
            "note": "Additional chart data (labs, notes, studies, medications) must be requested via tools.",
        }

    def _pick(self, options: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Choose among equally-valid options: seeded when an RNG was supplied
        (reproducible), else the first (stable). Never a bare ``random`` call."""
        if len(options) <= 1 or self._rng is None:
            return options[0]
        return options[self._rng.randrange(len(options))]

    def _earliest_panels(self, panels: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The visible panels sharing the earliest KNOWN offset (timing-verified ones
        preferred, so an unknown-timing panel never presents when a dated one exists)."""
        dated = [(self._item_offset(p), p) for p in panels if self._item_offset(p) is not None]
        if dated:
            lo = min(o for o, _ in dated)
            return [p for o, p in dated if o == lo]
        return list(panels)

    def _mark_revealed(self, panel_name: Optional[str]) -> None:
        # a LIST, not a set — this rides in ``info`` and is persisted/exported, so it
        # must stay JSON-serializable.
        if panel_name is not None and panel_name not in self.revealed["panels"]:
            self.revealed["panels"].append(panel_name)

    @staticmethod
    def _panel_public(panel: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "panel": panel.get("panel"),
            "collected_offset_days": panel.get("collected_offset_days", 0),
            "results": panel.get("results") or [],
        }

    # ─── Read-tool accessors (earnable zone — PRD §4 read tools) ──────────────
    # EVERY accessor goes through the temporal gate + the PHI scrub. A problem added
    # after the decision point IS the answer; a drug started after it reveals the
    # diagnosis (tolvaptan → SIADH, rasburicase → tumor lysis). These are the highest
    # -value leaks precisely because they occur on real, outcome-linked cases.
    def get_problem_list(self) -> List[Dict[str, Any]]:
        self.revealed["tools"].append("get_problem_list")
        return self._public_problems()

    def _public_problems(self) -> List[Dict[str, Any]]:
        visible, withheld = self._gated(self.case.get("problem_list"))
        out = [{"condition": self._scrub_guard(p.get("condition") or ""),
                "since": p.get("since")} for p in visible]
        if not out and withheld:
            return [self._withheld_marker("problems")]
        return out

    def get_vitals(self) -> Dict[str, Any]:
        self.revealed["tools"].append("get_vitals")
        return self._public_vitals()

    def _public_vitals(self) -> Dict[str, Any]:
        """Vitals collapse into ONE flat dict, so they carry ONE timing marker for the
        whole set (``collected_offset_days``, stamped by ``timeline`` from the latest
        vital-sign date and stripped here before the agent ever sees it).

        Gating is therefore all-or-nothing: a set KNOWN to post-date the decision
        point is withheld. An UNMARKED set is treated as the presenting vitals — the
        opening observation is defined as the presenting picture (PRD §8.4.2), and
        withholding it would make the environment unusable. This is the one place the
        cutoff is not fail-closed, so it is stated plainly rather than implied:
        per-timepoint vitals must be modeled as lab panels, which ARE gated
        individually and fail closed. Free text is always PHI-scrubbed."""
        vitals = self.case.get("vitals") or {}
        if not vitals:
            return {}
        if self._is_future(vitals):
            return dict(self._withheld_marker("vitals"))
        return {k: (self._scrub_guard(v) if isinstance(v, str) else v)
                for k, v in vitals.items() if k != "collected_offset_days"}

    def get_medications(self) -> List[Dict[str, Any]]:
        self.revealed["tools"].append("get_medications")
        visible, withheld = self._gated(self.case.get("medications"))
        out = [{"drug": self._scrub_guard(m.get("drug") or ""),
                "dose": m.get("dose"), "route": m.get("route"), "freq": m.get("freq")}
               for m in visible]
        if not out and withheld:
            return [self._withheld_marker("medications")]
        return out

    def get_labs(self, panel: Optional[str] = None) -> Dict[str, Any]:
        """Return a lab panel by name (or a directory of available panels). Only
        panels at/before the decision point are ever visible (temporal gate)."""
        panel = None if panel is None else str(panel)
        self.revealed["tools"].append(f"get_labs:{panel or '*'}")
        vis = self._visible_panels()
        if not panel:
            return {"available_panels": [p.get("panel") for p in vis]}
        want = panel.strip().lower()
        # Tiered matching so "renal" can't silently resolve to "adrenal": exact wins,
        # then word-boundary/prefix, then substring. The panel actually matched is
        # echoed back as ``matched_panel`` so the agent (and the verifier) can see
        # which request was served.
        exact = [p for p in vis if (p.get("panel") or "").strip().lower() == want]
        prefix = [p for p in vis if _has_word_prefix((p.get("panel") or "").lower(), want)]
        substr = [p for p in vis if want in (p.get("panel") or "").strip().lower()]
        for bucket in (exact, prefix, substr):
            if bucket:
                p = self._pick(bucket)   # seeded tie-break among equally-good matches
                self._mark_revealed(p.get("panel"))
                return {**self._panel_public(p), "matched_panel": p.get("panel"),
                        "requested": panel}
        # Honest not_available (PRD §8.4.6) — missing data is itself a clinical state.
        return {"panel": panel, "results": [], "status": "not_available",
                "available_panels": [p.get("panel") for p in vis]}

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
                vis = [p for p in vis if (self._item_offset(p) or 0) >= lo]
        # unknown-timing panels can only reach here on an authored case (the gate
        # withholds them on real ones), where day 0 is the authoring convention.
        buckets: Dict[int, List[Dict[str, Any]]] = {}
        for p in vis:
            off = self._item_offset(p)
            buckets.setdefault(0 if off is None else off, []).append(self._panel_public(p))
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
