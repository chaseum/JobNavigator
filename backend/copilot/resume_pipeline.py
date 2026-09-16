"""Tailored résumé content: facts -> plan -> safe rewrites -> deterministic checks + LLM audit.

The LLM selects facts and rewords bullets; it never produces headings. Employer,
title, school, degree, project name and dates are copied from facts by
`entry_head`. Every bullet carries source_fact_ids, and `check_bullet` rejects
what can be checked mechanically (uncited, out-of-entry citations, numbers and
tools the sources do not contain) before a second model audits the rest.
"""
import logging
import re

from backend.analyzer.llm_client import call_structured
from backend.copilot import facts as F
from backend.copilot import latex
from backend.copilot.schemas import BulletRewrites, ResumeAudit, ResumePlan

logger = logging.getLogger("jobnavigator.copilot.resume")

ENTRY_KINDS = ("experience", "internship", "research", "project")
SECTION_TITLES = {"education": "Education", "experience": "Experience", "research": "Research",
                  "projects": "Projects", "skills": "Skills", "certifications": "Certifications"}
DEFAULT_SECTION_ORDER = ["education", "experience", "research", "projects", "skills", "certifications"]
SEVERITY = {"SUPPORTED": 0, "AMBIGUOUS": 1, "UNSUPPORTED": 2}
_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ── facts ────────────────────────────────────────────────────────────────────

def date_label(value) -> str:
    v = (value or "").strip().lower()
    if v == "present":
        return "Present"
    if len(v) == 7:
        return f"{_MONTHS[int(v[5:]) - 1]} {v[:4]}"
    return v


def span_label(start, end) -> str:
    a, b = date_label(start), date_label(end)
    return f"{a} – {b}" if a and b else (a or b)


def _recency(f) -> str:
    d = f.data or {}
    end = (d.get("end_date") or d.get("graduation_date") or d.get("date") or "").lower()
    return "9999" if end == "present" else (end or d.get("start_date") or "0000")


class FactIndex:
    """Verified facts by provenance id, with parent/child and skill-usage links."""

    def __init__(self, facts):
        self.facts = list(facts)
        self.by_ref = {F.fact_ref(f): f for f in self.facts}
        self.children: dict = {}
        for f in self.facts:
            if f.parent_id is not None:
                self.children.setdefault(f.parent_id, []).append(f)

    def of_kind(self, *kinds):
        return [f for f in self.facts if f.kind in kinds]

    def allowed_for(self, ref: str) -> set:
        """What a bullet under this entry may cite: the entry, achievements under it, skills whose evidence names it."""
        f = self.by_ref.get(ref)
        if f is None:
            return set()
        out = {ref} | {F.fact_ref(c) for c in self.children.get(f.id, [])}
        out |= {F.fact_ref(s) for s in self.of_kind("skill") if ref in ((s.data or {}).get("evidence_ids") or [])}
        return out

    def text(self, refs) -> str:
        return "\n".join(F.fact_text(self.by_ref[r].kind, self.by_ref[r].data or {}) for r in refs if r in self.by_ref)


def base_bullets(ix: FactIndex, ref: str, limit: int | None = None) -> list[dict]:
    """The facts under an entry, word for word: the untailored baseline every diff is measured against."""
    f = ix.by_ref[ref]
    d = f.data or {}
    out = [{"text": c.data["text"], "source_fact_ids": [F.fact_ref(c)]}
           for c in ix.children.get(f.id, []) if c.kind == "achievement"]
    out += [{"text": r, "source_fact_ids": [ref]} for r in d.get("responsibilities") or []]
    if f.kind == "project" and not out and d.get("description"):
        out.append({"text": d["description"], "source_fact_ids": [ref]})
    if f.kind == "education":
        bits = ([f"GPA: {d['gpa']}"] if d.get("gpa") else []) + (["Honors: " + ", ".join(d["honors"])] if d.get("honors") else [])
        if bits:
            out.append({"text": "; ".join(bits), "source_fact_ids": [ref]})
        if d.get("coursework"):
            out.append({"text": "Coursework: " + ", ".join(d["coursework"]), "source_fact_ids": [ref]})
    return out[:limit] if limit else out


def entry_head(f) -> dict:
    """Heading fields copied from the fact. Nothing a model wrote ever reaches these."""
    d, k = f.data or {}, f.kind
    if k in ("experience", "internship"):
        return {"heading": d["employer"], "subheading": d["title"], "location": d.get("location", ""),
                "date": span_label(d.get("start_date"), d.get("end_date"))}
    if k == "research":
        return {"heading": d["organization"], "subheading": d.get("title") or d.get("research_area") or "Research",
                "location": d.get("location", ""), "date": span_label(d.get("start_date"), d.get("end_date"))}
    if k == "project":
        return {"heading": d["name"], "subheading": d.get("role") or ", ".join(d.get("technologies") or []),
                "location": "", "date": span_label(d.get("start_date"), d.get("end_date"))}
    if k == "education":
        sub = ", ".join(x for x in (d.get("degree"), d.get("major"), f"Minor in {d['minor']}" if d.get("minor") else "") if x)
        return {"heading": d["institution"], "subheading": sub, "location": d.get("location", ""),
                "date": date_label(d.get("graduation_date"))}
    if k == "certification":
        return {"heading": d["name"], "subheading": d.get("issuer", ""), "location": "", "date": date_label(d.get("date"))}
    raise ValueError(f"{k} is not a résumé entry")


def header_from(persona) -> dict:
    c = (getattr(persona, "contact", None) or {}) if persona is not None else {}
    name = " ".join(x for x in (c.get("preferred_name") or c.get("first_name"), c.get("last_name")) if x)
    items = []
    if c.get("email"):
        items.append({"text": c["email"], "url": f"mailto:{c['email']}"})
    if c.get("phone"):
        items.append({"text": c["phone"], "url": ""})
    place = ", ".join(x for x in (c.get("city"), c.get("state")) if x)
    if place:
        items.append({"text": place, "url": ""})
    for key in ("linkedin", "github", "portfolio", "website"):
        v = (c.get(key) or "").strip()
        if v:
            url = v if v.startswith("http") else f"https://{v}"
            items.append({"text": re.sub(r"^https?://(www\.)?", "", v).rstrip("/"), "url": url})
    return {"name": name or "Your Name", "contact": items}


def build_resume(ix: FactIndex, persona, bullets_by_entry: dict, project_refs: list, skill_refs: list,
                 section_order=None, template=latex.DEFAULT_TEMPLATE) -> dict:
    """Structured résumé JSON. Bullet ids are "<entry ref>:<n>"; entries not in bullets_by_entry keep their base bullets."""
    def entry(f, bullets):
        ref = F.fact_ref(f)
        return {"fact_id": ref, **entry_head(f),
                "bullets": [{"id": f"{ref}:{i + 1}", **b} for i, b in enumerate(bullets)]}

    def bullets_for(f):
        ref = F.fact_ref(f)
        return bullets_by_entry[ref] if ref in bullets_by_entry else base_bullets(ix, ref)

    sections = []
    for sid in section_order or DEFAULT_SECTION_ORDER:
        if sid == "skills":
            groups: dict = {}
            for ref in skill_refs:
                s = ix.by_ref.get(ref)
                if s is not None and s.kind == "skill":
                    g = groups.setdefault((s.data or {}).get("category") or "Skills", {"items": [], "source_fact_ids": []})
                    g["items"].append(s.data["name"])
                    g["source_fact_ids"].append(ref)
            if groups:
                sections.append({"id": "skills", "title": SECTION_TITLES["skills"],
                                 "lines": [{"label": k, **v} for k, v in groups.items()]})
            continue
        if sid == "experience":
            fs = sorted(ix.of_kind("experience", "internship"), key=_recency, reverse=True)
        elif sid == "research":
            fs = sorted(ix.of_kind("research"), key=_recency, reverse=True)
        elif sid == "education":
            fs = sorted(ix.of_kind("education"), key=_recency, reverse=True)
        elif sid == "certifications":
            fs = ix.of_kind("certification")
        elif sid == "projects":
            fs = [ix.by_ref[r] for r in project_refs if r in ix.by_ref]
        else:
            continue
        entries = [entry(f, [] if f.kind == "certification" else bullets_for(f)) for f in fs]
        if entries:
            sections.append({"id": sid, "title": SECTION_TITLES[sid], "entries": entries})
    return {"template": template, "header": header_from(persona), "sections": sections}


def recent_projects(ix: FactIndex, n=3) -> list[str]:
    return [F.fact_ref(f) for f in sorted(ix.of_kind("project"), key=_recency, reverse=True)[:n]]


def build_base(ix: FactIndex, persona, section_order=None, template=latex.DEFAULT_TEMPLATE, family=None) -> dict:
    """All verified facts, verbatim, up to five bullets an entry and the three most recent projects.

    With a `family` (backend/copilot/role_families.py) the same facts are kept but
    ordered for that role: the bullets, projects and skills the evidence itself
    supports for that kind of work lead. It is a selection over one truth, so every
    claim is still a fact's own words and still carries its provenance id — the
    family vocabulary decides what is shown, never what is said.
    """
    from backend.copilot.role_families import relevance

    def rank(items, text_of):
        """Most relevant first, ties in the original order; unchanged when there is no family."""
        if family is None:
            return list(items)
        return [x for _, _, x in sorted(((-relevance(family, text_of(x)), i, x) for i, x in enumerate(items)),
                                        key=lambda t: t[:2])]

    bullets = {}
    for f in ix.of_kind("experience", "internship", "research", "project"):
        ref = F.fact_ref(f)
        head = F.fact_headline(f.kind, f.data or {})
        bullets[ref] = rank(base_bullets(ix, ref), lambda b: f"{head} {b['text']}")[:5]

    projects = recent_projects(ix)
    if family is not None:
        ordered = rank(sorted(ix.of_kind("project"), key=_recency, reverse=True),
                       lambda p: F.fact_text(p.kind, p.data or {}))
        projects = [F.fact_ref(p) for p in ordered[:3]]
    skills = [F.fact_ref(s) for s in rank(ix.of_kind("skill"), lambda s: F.fact_text(s.kind, s.data or {}))]

    resume = build_resume(ix, persona, bullets, projects, skills, section_order, template)
    if family is not None:
        resume["role_family"] = family["id"]
    return resume


# ── deterministic claim checks ───────────────────────────────────────────────

_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
LEADERSHIP = ("led", "lead", "leading", "managed", "manager", "mentored", "supervised", "directed", "spearheaded", "headed", "oversaw")


def _numbers(text: str) -> set:
    return {n.replace(",", "") for n in _NUM_RE.findall(text or "")}


def has_term(term: str, text: str) -> bool:
    return bool(term) and re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", text or "", re.I) is not None


def check_bullet(bullet: dict, allowed: set, ix: FactIndex, jd_tech=(), jd_domain=()) -> list[dict]:
    """[{status, reason}] problems a machine can prove; empty means nothing found, not that the claim is verified."""
    src = [s for s in bullet.get("source_fact_ids") or []]
    if not src:
        return [{"status": "UNSUPPORTED", "reason": "no source facts cited"}]
    issues = []
    outside = [s for s in src if s not in allowed]
    if outside:
        issues.append({"status": "UNSUPPORTED", "reason": f"cites facts outside this entry: {', '.join(outside)}"})
    text, src_text = bullet.get("text") or "", ix.text([s for s in src if s in allowed])
    new_numbers = sorted(_numbers(text) - _numbers(src_text))
    if new_numbers:
        issues.append({"status": "UNSUPPORTED", "reason": f"number not in its sources: {', '.join(new_numbers)}"})
    tools = sorted({t for t in jd_tech if has_term(t, text) and not has_term(t, src_text)})
    if tools:
        issues.append({"status": "UNSUPPORTED", "reason": f"names {', '.join(tools)}, which its sources do not mention"})
    terms = sorted({t for t in jd_domain if has_term(t, text) and not has_term(t, src_text)})
    if terms:
        issues.append({"status": "AMBIGUOUS", "reason": f"uses the job's term {', '.join(terms)}; confirm it describes the source accurately"})
    if any(has_term(w, text) for w in LEADERSHIP) and not any(has_term(w, src_text) for w in LEADERSHIP):
        issues.append({"status": "AMBIGUOUS", "reason": "leadership wording its sources do not use"})
    if has_term("production", text) and not has_term("production", src_text):
        issues.append({"status": "AMBIGUOUS", "reason": "claims production use its sources do not state"})
    return issues


def worst(statuses) -> str:
    return max(statuses, key=SEVERITY.get, default="SUPPORTED")


def iter_bullets(resume: dict):
    for s in resume.get("sections") or []:
        for e in s.get("entries") or []:
            for b in e.get("bullets") or []:
                yield s, e, b


def summarize_audit(claims: dict) -> dict:
    rows = list(claims.values())
    counts = {k: sum(1 for c in rows if c["status"] == k) for k in SEVERITY}
    return {"claims": rows, "counts": counts,
            "blocked": counts["UNSUPPORTED"] > 0,
            "unreviewed": [c["bullet_id"] for c in rows if c["status"] == "AMBIGUOUS" and not c.get("reviewed")]}


AUDIT_SYSTEM = "You are a strict fact checker for résumés. You compare each bullet with only the source facts it cites."
AUDIT_PROMPT = """Check every bullet against ONLY its cited sources.

SUPPORTED: every tool, number, responsibility, scope and outcome in the bullet is stated in the sources.
AMBIGUOUS: plausible, but the wording stretches the sources (broader scope, stronger ownership, implied leadership, exposure presented as expertise).
UNSUPPORTED: the bullet states something the sources do not.

<<BULLETS>>"""


async def audit_resume(resume: dict, ix: FactIndex, jd_tech=(), jd_domain=(), use_llm=True, job_id=None) -> dict:
    claims = {}
    for sec in resume.get("sections") or []:
        for e in sec.get("entries") or []:
            allowed = ix.allowed_for(e.get("fact_id"))
            to_ask = []
            for b in e.get("bullets") or []:
                issues = check_bullet(b, allowed, ix, jd_tech, jd_domain)
                claims[b["id"]] = {"bullet_id": b["id"], "reasons": [i["reason"] for i in issues],
                                   "status": worst(i["status"] for i in issues), "reviewed": False}
                if use_llm and b.get("original") is not None and b["text"] != b["original"]:
                    to_ask.append(b)
            if not to_ask:
                continue
            block = "\n\n".join(
                f"bullet_id {b['id']}: \"{b['text']}\"\nsources:\n{ix.text([s for s in b['source_fact_ids'] if s in allowed]) or '(none)'}"
                for b in to_ask)
            try:
                # 2000 truncated qwen3:8b mid-reason on a four-bullet entry ("Unterminated string"), leaving the bullets unaudited
                out, _, _ = await call_structured(ResumeAudit, AUDIT_PROMPT.replace("<<BULLETS>>", block), AUDIT_SYSTEM,
                                                  feature="copilot", max_tokens=6000, job_id=job_id)
                verdicts = {c.bullet_id: c for c in out.claims}
            except Exception as ex:   # a failed audit means "review it yourself", never "passed"
                logger.warning(f"audit call failed: {ex}")
                verdicts = {}
            for b in to_ask:
                c, v = claims[b["id"]], verdicts.get(b["id"])
                if v is None:
                    c["reasons"].append("not audited by the model; review it yourself")
                    c["status"] = worst([c["status"], "AMBIGUOUS"])
                else:
                    c["status"] = worst([c["status"], v.status])
                    if v.status != "SUPPORTED" and v.reason:
                        c["reasons"].append(f"audit: {v.reason}")
    return summarize_audit(claims)


# ── the LLM steps ────────────────────────────────────────────────────────────

PLAN_SYSTEM = "You choose which verified facts belong on a résumé for one job. You select and group fact ids; you never write claims."
PLAN_PROMPT = """Choose the content of a résumé for this job.

Rules:
- Use only fact ids that appear in square brackets below, written without the brackets: "project_4", not "[project_4]".
- entries: the experience, internship and research entries to write bullets for, and the projects to show. <<PROJECT_RULE>>
- bullet_sources for an entry: at most <<MAX>> bullets. Each bullet is a list of ids from THAT entry: the entry id itself, achievements indented under it, or skills whose evidence names it. Prefer facts that support the requirements below, and quantified outcomes.
- skill_ids: skill facts most relevant to this job, most relevant first.

REQUIREMENTS (status, then the facts that support them):
<<EVIDENCE>>

GAPS TO CLOSE (verified facts the base résumé omits or under-shows for this job; prefer them):
<<GAPS>>

FACTS:
<<FACTS>>"""

REWRITE_SYSTEM = ("You rewrite résumé bullets using only the facts provided. You never add a tool, number, responsibility, "
                  "scope, leadership or production claim that the facts do not state.")
REWRITE_PROMPT = """Write <<N>> résumé bullets for this entry, in order.

ENTRY (fixed; do not mention a different employer, title, project or date): <<HEAD>>

FACTS YOU MAY USE:
<<FACTS>>

BULLETS (each line lists the fact ids that bullet must draw on):
<<GROUPS>>

JOB REQUIREMENTS THIS ENTRY SUPPORTS (use their wording only where it accurately describes the facts):
<<REQS>>

Rules:
- source_fact_ids: the ids the bullet actually uses, taken from its line above, without brackets ("achievement_12", not "[achievement_12]").
- requirement_ids: ids of the requirements the bullet speaks to, if any.
- You may clarify, shorten, reorder clauses, combine the facts on one line, and lead with the strongest real outcome.
- You may not invent tools, metrics, responsibilities or scope, claim leadership or production use the facts do not state, or turn exposure into expertise.
- One line each, under 190 characters, starting with a verb, no first person.
- reason: one short sentence on what you changed and why."""


def section_refs(resume: dict, section_id: str) -> list[str]:
    """The fact ids a built résumé shows in one section — how a tailored draft inherits its base's selection."""
    for s in (resume or {}).get("sections") or []:
        if s.get("id") != section_id:
            continue
        if section_id == "skills":
            return [r for line in s.get("lines") or [] for r in line.get("source_fact_ids") or []]
        return [e["fact_id"] for e in s.get("entries") or [] if e.get("fact_id")]
    return []


def validate_plan(plan: ResumePlan, ix: FactIndex, base_projects: list, policy: str, max_bullets: int,
                  skill_order: list | None = None):
    """(bullet groups by entry, project refs, skill refs, notes, planned refs) keeping only what the facts allow.

    `planned` are the entries the plan chose to write; every other entry keeps its facts' own words.
    """
    groups: dict = {}
    notes = []
    for pe in plan.entries:
        f = ix.by_ref.get(pe.fact_id)
        if f is None or f.kind not in ENTRY_KINDS:
            notes.append(f"dropped unknown entry {pe.fact_id}")
            continue
        allowed = ix.allowed_for(pe.fact_id)
        kept = [[s for s in dict.fromkeys(g) if s in allowed] for g in pe.bullet_sources]
        if any(s not in allowed for g in pe.bullet_sources for s in g):
            notes.append(f"dropped citations outside {pe.fact_id}")
        groups[pe.fact_id] = [g for g in kept if g][:max_bullets]

    planned_projects = [r for r in groups if ix.by_ref[r].kind == "project"]
    if policy == "keep":
        projects = list(base_projects)
    elif policy == "reorder":
        projects = [p for p in planned_projects if p in base_projects] + [p for p in base_projects if p not in planned_projects]
    else:
        projects = planned_projects[:3] or list(base_projects)
    for ref in list(groups):
        if ix.by_ref[ref].kind == "project" and ref not in projects:
            del groups[ref]
    # every role and research entry stays on the page; only its bullets are chosen
    for f in ix.of_kind("experience", "internship", "research"):
        groups.setdefault(F.fact_ref(f), [b["source_fact_ids"] for b in base_bullets(ix, F.fact_ref(f), max_bullets)])
    for ref in projects:
        if not groups.get(ref):
            groups[ref] = [b["source_fact_ids"] for b in base_bullets(ix, ref, max_bullets)]

    skills = [s for s in dict.fromkeys(plan.skill_ids) if s in ix.by_ref and ix.by_ref[s].kind == "skill"]
    # everything the plan did not name still shows, in the base résumé's order when
    # there is one — that is how a family base's skill selection reaches the draft
    rest = [r for r in (skill_order or []) if r in ix.by_ref and ix.by_ref[r].kind == "skill"]
    rest += [F.fact_ref(s) for s in ix.of_kind("skill") if F.fact_ref(s) not in rest]
    skills += [r for r in dict.fromkeys(rest) if r not in skills]
    planned = {pe.fact_id for pe in plan.entries if groups.get(pe.fact_id)}
    return groups, projects, skills, notes, planned


def original_text(ix: FactIndex, group: list) -> str:
    """The baseline wording for a bullet group: the base bullet of its first source."""
    first = group[0]
    f = ix.by_ref[first]
    if f.kind == "achievement":
        return f.data["text"]
    for b in base_bullets(ix, first):
        if b["source_fact_ids"] == [first]:
            return b["text"]
    d = f.data or {}
    if d.get("description"):
        return d["description"]
    if d.get("technologies"):
        return "Technologies: " + ", ".join(d["technologies"])
    return F.fact_headline(f.kind, d)


def surface_gaps(ix: FactIndex, groups: dict, projects: list, gaps, evidence, policy: str, max_bullets: int) -> list[str]:
    """Put SAFE_TO_ADD evidence the plan left off onto the page, so surfacing a verified omission never depends on the model noticing.

    Mutates `groups` and `projects`; returns notes. Skills and education are always on the page already.
    A full entry gives up a bullet that supports no requirement; if every bullet does, nothing is displaced.
    """
    supporting = {r for e in evidence or [] if e.get("status") in ("MATCHED", "PARTIAL") for r in e.get("source_fact_ids") or []}
    parents = {f.id: f for f in ix.facts}
    notes = []
    for g in gaps or []:
        if g.get("gap") != "SAFE_TO_ADD":
            continue
        for ref in g.get("profile_evidence") or []:
            f = ix.by_ref.get(ref)
            entry_fact = parents.get(f.parent_id) if f is not None and f.kind == "achievement" else f
            if entry_fact is None or entry_fact.kind not in ENTRY_KINDS:
                continue
            entry = F.fact_ref(entry_fact)
            if entry_fact.kind == "project" and entry not in projects:
                if policy == "keep":
                    notes.append(f"{entry} supports {g['requirement_id']}, but the project policy keeps the base projects")
                    continue
                if len(projects) >= 3:
                    drop = next((p for p in reversed(projects) if not (ix.allowed_for(p) & supporting)), None)
                    if drop is None:
                        notes.append(f"no room for {entry}: every shown project supports a requirement")
                        continue
                    projects.remove(drop)
                    groups.pop(drop, None)
                    notes.append(f"replaced project {drop} with {entry} for {g['requirement_id']}")
                projects.append(entry)
                groups[entry] = [b["source_fact_ids"] for b in base_bullets(ix, entry, max_bullets)]
            grp = groups.setdefault(entry, [])
            if any(ref in x for x in grp):
                continue
            if len(grp) >= max_bullets:
                idx = next((i for i in range(len(grp) - 1, -1, -1) if not set(grp[i]) & supporting), None)
                if idx is None:
                    notes.append(f"no room under {entry} for {ref}: every bullet supports a requirement")
                    continue
                grp[idx] = [ref]
            else:
                grp.append([ref])
            notes.append(f"added {ref} under {entry} for {g['requirement_id']}")
    return notes


async def rewrite_entry(ix: FactIndex, ref: str, groups: list, requirements: list, evidence_by_req: dict, job_id=None):
    allowed = ix.allowed_for(ref)
    relevant = [r for r in requirements if set((evidence_by_req.get(r["id"]) or {}).get("source_fact_ids") or []) & allowed]
    prompt = (REWRITE_PROMPT.replace("<<N>>", str(len(groups)))
              .replace("<<HEAD>>", F.fact_headline(ix.by_ref[ref].kind, ix.by_ref[ref].data or {}))
              .replace("<<FACTS>>", "\n".join(f"[{r}] {ix.text([r])}" for r in sorted(allowed)))
              .replace("<<GROUPS>>", "\n".join(f"{i + 1}. [{', '.join(g)}]" for i, g in enumerate(groups)))
              .replace("<<REQS>>", "\n".join(f"{r['id']}: {r['text']}" for r in relevant) or "(none)"))
    out, provider, model = await call_structured(BulletRewrites, prompt, REWRITE_SYSTEM, feature="copilot",
                                                 max_tokens=2000, job_id=job_id)
    known = {r["id"] for r in requirements}
    bullets = []
    for i, g in enumerate(groups):
        orig = original_text(ix, g)
        if i < len(out.bullets):
            rw = out.bullets[i]
            bullets.append({"text": " ".join(rw.text.split()), "source_fact_ids": list(dict.fromkeys(rw.source_fact_ids)),
                            "planned_sources": g, "requirement_ids": [x for x in rw.requirement_ids if x in known],
                            "reason": rw.reason, "original": orig})
        else:
            bullets.append({"text": orig, "source_fact_ids": g, "planned_sources": g, "requirement_ids": [],
                            "reason": "kept the original wording (the model returned fewer bullets)", "original": orig})
    return bullets, provider, model


async def tailor(ix: FactIndex, persona, analysis: dict, evidence: list, settings: dict, job_id=None, gaps=None,
                 base_resume: dict | None = None, role_family: str | None = None):
    """(resume_json, audit, provider, model) for one job; `gaps` are the base résumé's SAFE_TO_ADD / SAFE_TO_REPHRASE rows.

    `base_resume` is the role-family base this draft derives from: its project and
    skill selection is what tailoring starts from, so a PM application is tailored
    out of the PM selection rather than out of a universal résumé.
    """
    reqs = analysis.get("requirements") or []
    ev_by = {e["requirement_id"]: e for e in evidence or []}
    evidence_text = "\n".join(
        f"{r['id']} [{(ev_by.get(r['id']) or {}).get('status', 'UNKNOWN')}] {r['text']} <- "
        f"{', '.join((ev_by.get(r['id']) or {}).get('source_fact_ids') or []) or 'nothing'}" for r in reqs)
    # the base this draft derives from decides which projects and skills it starts with
    base_projects = [r for r in section_refs(base_resume or {}, "projects") if r in ix.by_ref] or recent_projects(ix)
    base_skills = [r for r in section_refs(base_resume or {}, "skills") if r in ix.by_ref]
    policy = settings.get("project_policy", "reorder")
    project_rule = {
        "keep": f"Show exactly these projects: {', '.join(base_projects) or 'none'}.",
        "reorder": f"Show only these projects, most relevant first: {', '.join(base_projects) or 'none'}.",
    }.get(policy, "Choose up to 3 projects from any project fact.")
    max_bullets = 4 if int(settings.get("page_target") or 1) <= 1 else 6

    plan, provider, model = await call_structured(
        ResumePlan,
        PLAN_PROMPT.replace("<<PROJECT_RULE>>", project_rule).replace("<<MAX>>", str(max_bullets))
        .replace("<<EVIDENCE>>", evidence_text or "(none)").replace("<<FACTS>>", F.facts_prompt(ix.facts))
        .replace("<<GAPS>>", "\n".join(f"{g['requirement_id']} {g['gap']}: {g['requirement']} <- {', '.join(g.get('profile_evidence') or []) or 'nothing'}"
                                       for g in gaps or []) or "(none)"),
        PLAN_SYSTEM, feature="copilot", max_tokens=3000, job_id=job_id)
    groups, projects, skills, notes, planned = validate_plan(plan, ix, base_projects, policy, max_bullets, base_skills)
    notes += surface_gaps(ix, groups, projects, gaps, evidence, policy, max_bullets)
    if settings.get("skill_ordering") == "profile":
        order = {F.fact_ref(s): i for i, s in enumerate(ix.of_kind("skill"))}
        skills.sort(key=order.get)

    bullets_by_entry = {}
    for ref, g in groups.items():
        if ref in planned:
            bullets_by_entry[ref], provider, model = await rewrite_entry(ix, ref, g, reqs, ev_by, job_id)
        else:
            # not chosen for rewriting: the facts' own words, no model call
            bullets_by_entry[ref] = [{"text": original_text(ix, grp), "source_fact_ids": grp, "planned_sources": grp,
                                      "requirement_ids": [], "reason": "unchanged", "original": original_text(ix, grp)}
                                     for grp in g]
    resume = build_resume(ix, persona, bullets_by_entry, projects, skills, settings.get("section_order"), settings.get("template", latex.DEFAULT_TEMPLATE))
    resume["plan_notes"] = notes
    if role_family:
        resume["role_family"] = role_family
    audit = await audit_resume(resume, ix, analysis.get("technologies") or [], analysis.get("domain_terms") or [], True, job_id)
    return resume, audit, provider, model
