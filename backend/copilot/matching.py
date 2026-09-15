"""Role Match: deterministic scoring of LLM-extracted requirements against cited evidence.

The LLM extracts requirements and proposes which facts support each one. This
module decides what that is worth, and it trusts nothing it cannot check:
a MATCHED with no citation, or citing a fact that does not exist, earns nothing;
a skill with no usage evidence earns partial credit at most; work authorization
comes from the user's own answers, never the model. The number is evidence
coverage, not any employer's ATS score.
"""
import re

DEFAULT_WEIGHTS = {
    "eligibility": 30,     # hard requirements: authorization, clearance, required degree / years
    "required": 30,        # required qualifications
    "preferred": 10,       # preferred qualifications
    "experience": 15,      # responsibilities backed by experience
    "technology": 10,      # technologies and domain terms
    "parser_health": 5,    # the generated PDF parses cleanly
}
CREDIT = {"MATCHED": 1.0, "PARTIAL": 0.5, "UNKNOWN": 0.0, "MISSING": 0.0}
_ELIGIBILITY = {"work_authorization", "clearance"}
_HARD_WHEN_REQUIRED = {"education", "experience"}


def components_for(req: dict) -> list[str]:
    cat, required = req.get("category"), bool(req.get("required"))
    if cat in _ELIGIBILITY or (required and cat in _HARD_WHEN_REQUIRED):
        return ["eligibility"]
    if cat == "responsibility":
        return ["experience"]
    base = ["required" if required else "preferred"]
    if cat in ("technology", "domain"):
        base.append("technology")   # a required technology is both a requirement and terminology
    return base


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


def sanitize_evidence(requirements: list[dict], matches: list[dict], facts_by_ref: dict, identity: dict) -> list[dict]:
    """One checked evidence row per requirement, in requirement order.

    `facts_by_ref` maps verified provenance ids to {"kind", "data"}; `identity`
    maps "identity.<key>" to the user's answer.
    """
    by_req = {}
    for m in matches or []:
        by_req.setdefault(m.get("requirement_id"), m)
    out = []
    for req in requirements:
        rid = req["id"]
        m = by_req.get(rid) or {}
        status = m.get("status") if m.get("status") in CREDIT else "UNKNOWN"
        note = m.get("explanation") or ""
        cited = [s for s in dict.fromkeys(m.get("source_fact_ids") or [])]
        valid = [s for s in cited if s in facts_by_ref or s in identity]
        dropped = [s for s in cited if s not in valid]

        if req.get("category") == "work_authorization":
            status, valid = _work_auth_verdict(req.get("text"), identity)
            note = "from your work-authorization answers" if valid else "answer work authorization in your Profile"
        elif status in ("MATCHED", "PARTIAL") and not valid:
            status, note = "MISSING", "no verified fact was cited"
        elif status == "MATCHED" and all(
            facts_by_ref.get(s, {}).get("kind") == "skill" and not (facts_by_ref[s].get("data") or {}).get("evidence_ids")
            for s in valid
        ):
            # listing a skill is not having used it
            status, note = "PARTIAL", "skill listed without linked usage evidence"
        if status in ("MISSING", "UNKNOWN") and req.get("category") != "work_authorization":
            valid = []
        row = {"requirement_id": rid, "status": status, "source_fact_ids": valid, "explanation": note}
        if dropped:
            row["dropped_ids"] = dropped
        out.append(row)
    return out


def score(requirements: list[dict], evidence: list[dict], weights: dict | None = None, parser_health: float | None = None) -> dict:
    """Overall 0-100 plus every component; a component with nothing to measure is left out and the rest re-weighted."""
    weights = {**DEFAULT_WEIGHTS, **(weights or {})}
    status = {e["requirement_id"]: e["status"] for e in evidence}
    comps = {name: {"earned": 0.0, "possible": 0.0} for name in DEFAULT_WEIGHTS}
    blockers = []
    for req in requirements:
        st = status.get(req["id"], "UNKNOWN")
        w = int(req.get("importance") or 2)
        for c in components_for(req):
            comps[c]["possible"] += w
            comps[c]["earned"] += w * CREDIT[st]
        if "eligibility" in components_for(req) and st == "MISSING":
            blockers.append(req["text"])
    if parser_health is not None:
        comps["parser_health"] = {"earned": max(0.0, min(100.0, parser_health)) / 100, "possible": 1.0}

    active = {c: float(weights.get(c) or 0) for c, v in comps.items() if v["possible"] > 0}
    total = sum(active.values())
    overall = sum(active[c] * comps[c]["earned"] / comps[c]["possible"] for c in active) / total * 100 if total else 0.0
    counts = {k: 0 for k in CREDIT}
    for req in requirements:
        counts[status.get(req["id"], "UNKNOWN")] += 1
    return {
        "score": round(overall),
        "components": [
            {"name": c, "weight": weights.get(c, 0), "effective_weight": round(active[c] / total * 100, 1) if c in active and total else 0,
             "coverage": round(comps[c]["earned"] / comps[c]["possible"], 3) if comps[c]["possible"] else None}
            for c in DEFAULT_WEIGHTS
        ],
        "counts": counts,
        "hard_blockers": blockers,
    }


_STOP = {"with", "and", "the", "for", "from", "that", "this", "have", "experience", "years", "year", "using",
         "strong", "knowledge", "ability", "working", "work", "including", "skills", "understanding", "familiarity", "plus"}


def _terms(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9+#.]+", (text or "").lower()) if len(w) > 2 and w not in _STOP]


def gaps(requirements: list[dict], evidence: list[dict], resume_text: str | None, resume_refs: set | None) -> list[dict]:
    """Safe to add / safe to rephrase / cannot claim / needs clarification, per requirement.

    `resume_refs` are the fact ids the current résumé already uses (None when there
    is no résumé yet, so every supported requirement is "safe to add").
    """
    by_id = {e["requirement_id"]: e for e in evidence}
    text = (resume_text or "").lower()
    out = []
    for req in requirements:
        e = by_id.get(req["id"], {"status": "UNKNOWN", "source_fact_ids": []})
        src = e.get("source_fact_ids") or []
        base = {"requirement_id": req["id"], "requirement": req["text"], "source_fact_ids": src}
        if e["status"] == "MISSING":
            out.append({**base, "kind": "cannot_claim", "note": "No fact in your profile supports this. It will not be added to a résumé."})
        elif e["status"] in ("UNKNOWN", "PARTIAL"):
            out.append({**base, "kind": "needs_clarification",
                        "note": e.get("explanation") or "Add detail to your profile if you have real experience here."})
        else:
            facts_src = [s for s in src if not s.startswith("identity.")]
            if not facts_src:
                continue
            if resume_refs is None or any(s not in resume_refs for s in facts_src):
                out.append({**base, "kind": "safe_to_add", "note": "Supported by your profile but not on your current résumé."})
            else:
                terms = _terms(req["text"])
                present = sum(1 for t in terms if t in text)
                if terms and present / len(terms) < 0.6:
                    out.append({**base, "kind": "safe_to_rephrase",
                                "note": "Your résumé covers this with different wording; the job's terms can be used where accurate."})
    return out
