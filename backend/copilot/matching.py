"""Candidate Fit: how much of a job the whole verified profile satisfies.

The LLM extracts requirements and proposes which facts support each one. This
module decides what that is worth and trusts nothing it cannot check: every
citation is normalized to a canonical provenance id and must name a verified
fact; a MATCHED with no valid citation earns nothing; a skill with no usage
evidence is partial at most; work authorization comes from the user's own
answers. Then the deterministic rules in rules.py (degrees, technologies,
experience types, years) raise, cap or replace the model's verdict, so a model
that says MISSING never outvotes a degree the profile proves.

The number is requirement coverage, not any employer's ATS score. Parser Health
is not part of it, and neither is any legacy CV score.
"""
import re
from datetime import date

from backend.copilot import facts as F
from backend.copilot import rules as R

# One weight table for Candidate Fit and Resume Applicability. Settings key role_match_weights overrides it.
DEFAULT_WEIGHTS = {
    "required": 3.0,         # per required requirement, times its importance (1-3)
    "preferred": 1.0,        # per preferred requirement, times its importance
    "soft_skill": 0.5,       # multiplier: soft skills are rarely provable from facts
    "responsibility": 0.5,   # multiplier: what the job does, not what it demands of you
}
CREDIT = {"MATCHED": 1.0, "PARTIAL": 0.5, "UNKNOWN": 0.0, "MISSING": 0.0}
ELIGIBILITY = {"work_authorization", "clearance"}
FORMULA = ("weight = (required or preferred weight) × importance (1–3) × soft-skill / responsibility multiplier; "
           "score = Σ weight × credit ÷ Σ weight × 100. Work authorization and clearance are eligibility checks, "
           "listed separately and never scored.")


def requirement_weight(req: dict, weights: dict) -> float:
    w = float(weights["required"] if req.get("required") else weights["preferred"]) * int(req.get("importance") or 2)
    if req.get("category") == "soft_skill":
        w *= float(weights["soft_skill"])
    elif req.get("category") == "responsibility":
        w *= float(weights["responsibility"])
    return w


def _work_auth_verdict(text: str, identity: dict):
    """(status, source ids) for a work-authorization requirement from the user's answers; UNKNOWN when unanswered."""
    t = (text or "").lower()
    needs_sponsor = [k for k in ("identity.requires_sponsorship_now", "identity.requires_sponsorship_future") if identity.get(k) is True]
    no_sponsor = [k for k in ("identity.requires_sponsorship_now", "identity.requires_sponsorship_future") if identity.get(k) is False]
    authorized = identity.get("identity.authorized_us")
    if "sponsor" in t:
        if needs_sponsor:
            return "MISSING", needs_sponsor
        if no_sponsor and authorized is True:
            return "MATCHED", no_sponsor + ["identity.authorized_us"]
        return "UNKNOWN", []
    if authorized is True:
        return "MATCHED", ["identity.authorized_us"]
    if authorized is False:
        return "MISSING", ["identity.authorized_us"]
    return "UNKNOWN", []


def check_citations(cited, facts_by_ref: dict, identity: dict) -> tuple[list, list]:
    """(canonical verified ids, dropped raw strings) for what the model cited."""
    valid, dropped = [], []
    for raw in cited or []:
        ref = F.normalize_ref(raw)
        if ref is not None and (ref in facts_by_ref or ref in identity):
            if ref not in valid:
                valid.append(ref)
        else:
            dropped.append(raw)
    return valid, dropped


def sanitize_evidence(requirements: list[dict], matches: list[dict], facts_by_ref: dict, identity: dict,
                      jd_technologies=(), today: date | None = None) -> list[dict]:
    """One checked evidence row per requirement, in requirement order.

    `facts_by_ref` maps verified provenance ids to {"kind", "data", "parent"};
    `identity` maps "identity.<key>" to the user's answer. Each row keeps the
    model's raw verdict and citations next to the result, so every status change
    can be traced.
    """
    today = today or date.today()
    units = R.profile_units(facts_by_ref)
    vocab = R.vocabulary(facts_by_ref, jd_technologies)
    by_req = {}
    for m in matches or []:
        by_req.setdefault(m.get("requirement_id"), m)
    out = []
    for req in requirements:
        rid = req["id"]
        m = by_req.get(rid) or {}
        llm_status = m.get("status") if m.get("status") in CREDIT else None
        status = llm_status or "UNKNOWN"
        note = m.get("explanation") or ""
        original = list(m.get("source_fact_ids") or [])
        valid, dropped = check_citations(original, facts_by_ref, identity)

        if req.get("category") == "work_authorization":
            status, valid = _work_auth_verdict(req.get("text"), identity)
            note = "from your work-authorization answers" if valid else "answer work authorization in your Profile"
        elif status in ("MATCHED", "PARTIAL") and not valid:
            status, note = "MISSING", "the model cited no verified fact" + (f" (dropped: {', '.join(map(str, dropped))})" if dropped else "")
        elif status == "MATCHED" and all(
            facts_by_ref.get(s, {}).get("kind") == "skill" and not (facts_by_ref[s].get("data") or {}).get("evidence_ids")
            for s in valid
        ):
            # listing a skill is not having used it
            status, note = "PARTIAL", "skill listed without linked usage evidence"
        if status in ("MISSING", "UNKNOWN") and req.get("category") != "work_authorization":
            valid = []
        row = {"requirement_id": rid, "status": status, "source_fact_ids": valid, "explanation": note,
               "llm_status": llm_status, "llm_explanation": m.get("explanation") or "",
               "original_source_fact_ids": original,
               "normalized_source_fact_ids": [F.normalize_ref(s) for s in original if F.normalize_ref(s)],
               "dropped_source_fact_ids": dropped}
        if req.get("category") not in ELIGIBILITY:
            row = R.combine(row, R.evaluate(req, units, vocab, today))
        out.append(row)
    return out


def score(requirements: list[dict], evidence: list[dict], weights: dict | None = None) -> dict:
    """Candidate Fit 0-100 (None when nothing is scorable), required/preferred coverage and hard blockers."""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    status = {e["requirement_id"]: e["status"] for e in evidence}
    earned = possible = 0.0
    cov = {"required": [0.0, 0.0], "preferred": [0.0, 0.0]}
    counts = {k: 0 for k in CREDIT}
    blockers = []
    for req in requirements:
        st = status.get(req["id"], "UNKNOWN")
        counts[st] += 1
        if req.get("category") in ELIGIBILITY:
            if st == "MISSING":
                blockers.append(req["text"])
            continue
        if req.get("required") and req.get("category") == "education" and st == "MISSING":
            blockers.append(req["text"])
        rw = requirement_weight(req, w)
        earned += rw * CREDIT[st]
        possible += rw
        c = cov["required" if req.get("required") else "preferred"]
        c[0] += rw * CREDIT[st]
        c[1] += rw
    return {
        "score": round(earned / possible * 100) if possible else None,
        "coverage": {k: (round(a / b, 3) if b else None) for k, (a, b) in cov.items()},
        "counts": counts,
        "hard_blockers": blockers,
        "weights": w,
        "formula": FORMULA,
    }


_STOP = {"with", "and", "the", "for", "from", "that", "this", "have", "experience", "years", "year", "using",
         "strong", "knowledge", "ability", "working", "work", "including", "skills", "understanding", "familiarity", "plus"}


def terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9+#.]+", (text or "").lower()) if len(w) > 2 and w not in _STOP]
