"""Resume Applicability: how much of what the verified profile supports THIS résumé visibly shows.

Deterministic, no model call. Candidate evidence rows (matching.sanitize_evidence)
say what the profile supports. This module checks what a specific résumé shows,
with the same rules over the résumé's own text and provenance, and scores it with
the same weights as Candidate Fit. A résumé can never earn more for a requirement
than the profile supports, so

    applicability <= maximum achievable == Candidate Fit   (page length aside)

Bullets that failed the claim audit, or still await review, show nothing.
"""
from datetime import date

from backend.copilot import matching as M
from backend.copilot import rules as R

RESUME_CREDIT = {"PRESENT": 1.0, "WEAK": 0.5, "OMITTED_SUPPORTED": 0.0, "UNSUPPORTED": 0.0, "UNKNOWN": 0.0}
GAP_KINDS = ("ALREADY_VISIBLE", "SAFE_TO_ADD", "SAFE_TO_REPHRASE", "NEEDS_CONTEXT", "CANNOT_CLAIM")
TARGET = 90
DISCLAIMER = ("Job-specific résumé coverage simulation. Employers and ATS products use different screening and ranking "
              "rules; this is not a score reported by any employer's ATS.")
_RANKED = ["OMITTED_SUPPORTED", "WEAK", "PRESENT"]


def visible_refs(units: list) -> set:
    """Refs whose evidence a reader can see: bullet and skill sources, and education headings (degree and date are the heading)."""
    return {r for u in units if not u["heading"] or u["kind"] == "education" for r in u["refs"]}


def excluded_bullets(resume: dict, claims: dict | None) -> list[str]:
    claims = claims or {}
    return [b["id"] for s in resume.get("sections") or [] for e in s.get("entries") or [] for b in e.get("bullets") or []
            if (claims.get(b.get("id")) or {}).get("status") == "UNSUPPORTED"
            or ((claims.get(b.get("id")) or {}).get("status") == "AMBIGUOUS" and not claims[b["id"]].get("reviewed"))]


def question_for(req: dict) -> str:
    return (f"The posting asks for: “{req.get('text')}”. Have you actually done this in an internship, job, project, "
            "research or coursework? If yes, describe what you did and where. Only add what is true.")


def _resume_status(req, cand, units, visible, vocab, today):
    cs = cand.get("status")
    if cs == "MISSING":
        return "UNSUPPORTED", [], False
    if cs not in ("MATCHED", "PARTIAL"):
        return "UNKNOWN", [], False
    cited = [r for r in cand.get("source_fact_ids") or [] if not r.startswith("identity.")]
    shown = [r for r in cited if r in visible]
    cite_rank = 2 if cited and len(shown) == len(cited) else 1 if shown else 0
    det = R.combine({"status": "UNKNOWN", "source_fact_ids": [], "explanation": ""}, R.evaluate(req, units, vocab, today))
    det_rank = {"MATCHED": 2, "PARTIAL": 1}.get(det["status"], 0)
    if not cited and det_rank == 0:
        return "UNKNOWN", [], False
    rank = min(max(cite_rank, det_rank), 2 if cs == "MATCHED" else 1)
    return _RANKED[rank], (shown if cite_rank > det_rank else det["source_fact_ids"] or shown), det_rank >= rank


def _gap(req, cand, status, visible, text, by_rule=False):
    cs = cand.get("status")
    omitted = [r for r in cand.get("source_fact_ids") or [] if not r.startswith("identity.") and r not in visible]
    if status == "UNSUPPORTED":
        return "CANNOT_CLAIM", "Nothing in your verified profile supports this, so tailoring will not add it."
    if status == "UNKNOWN":
        return "NEEDS_CONTEXT", cand.get("explanation") or "Your profile does not say enough to tell."
    if status == "OMITTED_SUPPORTED":
        return "SAFE_TO_ADD", (f"Verified in your profile ({', '.join(omitted[:5])}) but not shown on this résumé."
                               + (" The profile supports it only partly, so adding it earns partial credit." if cs == "PARTIAL" else ""))
    if status == "WEAK" and cs == "MATCHED":
        if omitted:
            return "SAFE_TO_ADD", f"The résumé shows part of the evidence; {', '.join(omitted[:5])} is verified but not shown."
        return "SAFE_TO_REPHRASE", "Shown only indirectly; accurate wording from the posting could make the evidence clearer."
    if status == "WEAK":
        return "NEEDS_CONTEXT", f"Your profile supports this only partly: {cand.get('explanation') or 'see Evidence'}"
    if by_rule:
        # the degree, dates or tools the rule matched are on the page as written; rewording adds nothing
        return "ALREADY_VISIBLE", "Proven from what this résumé shows (degree, dates or tools named in verified lines)."
    words = M.terms(req.get("text"))
    overlap = sum(1 for t in words if t in text) / len(words) if words else 1.0
    if overlap >= 0.6:
        return "ALREADY_VISIBLE", "Supported by your profile and shown on this résumé in the posting's terms."
    return "SAFE_TO_REPHRASE", "Shown on this résumé in different words; the posting's terms may be used where they describe the facts accurately."


def audit(requirements: list, evidence: list, resume: dict, facts_by_ref: dict, claims: dict | None = None,
          weights: dict | None = None, jd_technologies=(), today: date | None = None) -> dict:
    """Per-requirement résumé status and gap, the Resume Applicability score and the maximum the profile supports."""
    today = today or date.today()
    w = {**M.DEFAULT_WEIGHTS, **(weights or {})}
    units = R.resume_units(resume or {}, facts_by_ref, claims)
    visible = visible_refs(units)
    vocab = R.vocabulary(facts_by_ref, jd_technologies)
    text = "\n".join(u["text"] for u in units).lower()
    ev = {e["requirement_id"]: e for e in evidence or []}
    rows = []
    for req in requirements:
        cand = ev.get(req["id"]) or {"status": "UNKNOWN", "source_fact_ids": []}
        row = {"requirement_id": req["id"], "requirement": req.get("text"), "source_quote": req.get("source_quote", ""),
               "category": req.get("category"), "required": bool(req.get("required")), "importance": int(req.get("importance") or 2),
               "candidate_status": cand.get("status"), "profile_evidence": list(cand.get("source_fact_ids") or []),
               "candidate_note": cand.get("explanation", "")}
        if req.get("category") in M.ELIGIBILITY:
            rows.append({**row, "weight": 0, "resume_status": None, "resume_evidence": [], "gap": None,
                         "reason": "Eligibility comes from your Profile answers, not from résumé wording; it is never scored."})
            continue
        status, shown, by_rule = _resume_status(req, cand, units, visible, vocab, today)
        gap, reason = _gap(req, cand, status, visible, text, by_rule)
        rw = M.requirement_weight(req, w)
        row.update(weight=rw, resume_status=status, resume_evidence=shown, gap=gap, reason=reason,
                   credit=RESUME_CREDIT[status], max_credit=M.CREDIT.get(cand.get("status"), 0.0))
        if gap in ("NEEDS_CONTEXT", "CANNOT_CLAIM"):
            row["question"] = question_for(req)
        rows.append(row)

    scored = [r for r in rows if r["resume_status"] is not None]
    total = sum(r["weight"] for r in scored)
    pct = lambda x: round(x / total * 100) if total else None   # noqa: E731
    for r in scored:
        r["points_now"] = round((r["max_credit"] - r["credit"]) * r["weight"] / total * 100, 1) if total else 0
        r["points_with_context"] = round((1 - r["max_credit"]) * r["weight"] / total * 100, 1) if total else 0
    cov = {}
    for key, req_flag in (("required", True), ("preferred", False)):
        part = [r for r in scored if r["required"] is req_flag]
        tw = sum(r["weight"] for r in part)
        cov[key] = {"resume": round(sum(r["weight"] * r["credit"] for r in part) / tw, 3) if tw else None,
                    "profile": round(sum(r["weight"] * r["max_credit"] for r in part) / tw, 3) if tw else None}
    score, maximum = pct(sum(r["weight"] * r["credit"] for r in scored)), pct(sum(r["weight"] * r["max_credit"] for r in scored))
    if maximum is None:
        message = "No scorable requirements."
    elif maximum >= TARGET:
        message = (f"This résumé already shows {score}% of what the posting asks for."
                   if score >= TARGET else f"Truthful changes from your verified Profile could move this résumé from {score}% toward {maximum}%.")
    else:
        message = (f"{TARGET}–95% is not currently achievable without additional truthful experience or context: "
                   f"your verified Profile supports at most {maximum}%.")
    return {"score": score, "maximum": maximum, "target": TARGET, "target_reachable": maximum is not None and maximum >= TARGET,
            "message": message, "coverage": cov, "rows": rows,
            "gap_counts": {k: sum(1 for r in rows if r["gap"] == k) for k in GAP_KINDS},
            "status_counts": {k: sum(1 for r in scored if r["resume_status"] == k) for k in RESUME_CREDIT},
            "excluded_bullets": excluded_bullets(resume or {}, claims),
            "weights": w, "formula": M.FORMULA.replace("credit", "credit (PRESENT 1, WEAK 0.5, else 0)", 1), "disclaimer": DISCLAIMER}


def compare(before: dict, after: dict) -> dict:
    """What changed between two audits of the same analysis, and which verified facts caused each change."""
    old = {r["requirement_id"]: r for r in before.get("rows") or []}
    total = sum(r["weight"] for r in after.get("rows") or [] if r["resume_status"] is not None)
    changes = []
    for r in after.get("rows") or []:
        o = old.get(r["requirement_id"])
        if r["resume_status"] is None or o is None or o["resume_status"] == r["resume_status"]:
            continue
        points = round((r["credit"] - o["credit"]) * r["weight"] / total * 100, 1) if total else 0
        changes.append({"requirement_id": r["requirement_id"], "requirement": r["requirement"], "from": o["resume_status"],
                        "to": r["resume_status"], "points": points,
                        # an improvement is caused by evidence that appeared; a drop by evidence that is no longer shown
                        "caused_by": [x for x in r["resume_evidence"] if x not in o["resume_evidence"]] if points > 0 else [],
                        "lost": [x for x in o["resume_evidence"] if x not in r["resume_evidence"]] if points < 0 else []})
    changes.sort(key=lambda c: -c["points"])
    delta = (after["score"] - before["score"]) if after.get("score") is not None and before.get("score") is not None else None
    return {"before": before.get("score"), "after": after.get("score"), "maximum": after.get("maximum"), "delta": delta, "changes": changes}
