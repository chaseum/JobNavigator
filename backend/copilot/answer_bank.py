"""Answer Bank: saved application answers, matched by normalized question and intent.

Entries live in Persona.qa_bank (the old {question, answer} rows still read). The
stored shape is {id, question, aliases, answer, answer_type, user_verified,
last_used, created_at}; `normalized` and `intent` are derived on every read, never
trusted from storage. Protected intents (authorization, sponsorship, EEO,
salary, legal attestations) are never drafted by a model and are only reused
when the user saved and verified the answer themselves.
"""
import hashlib
import re
import uuid
from datetime import datetime, timezone

# (intent, protected, merge equivalent wordings into one entry, patterns)
INTENTS = [
    ("sexual_orientation", True, True, [r"sexual orientation"]),
    ("transgender", True, True, [r"\btransgender\b"]),
    ("gender", True, True, [r"\bgender\b", r"\bsex\b"]),
    ("race_ethnicity", True, True, [r"\brace\b", r"\bethnic", r"\bhispanic\b", r"\blatin[oax]\b"]),
    ("veteran_status", True, True, [r"\bveteran", r"military (service|status)", r"armed forces"]),
    ("disability_status", True, True, [r"\bdisabilit"]),
    ("over_18", True, True, [r"\b18 years", r"\bover (the age of )?18\b", r"\bat least 18\b"]),
    ("salary", True, True, [r"\bsalary", r"\bcompensation", r"\bpay (expectation|range|requirement)", r"desired (pay|wage)", r"expected (pay|wage)"]),
    ("criminal_background", True, False, [r"\bconvicted\b", r"\bcriminal\b", r"\bfelony\b"]),
    ("legal_attestation", True, False, [r"\bcertify\b", r"\battest", r"true,? (correct|accurate),? and complete", r"electronic signature"]),
    ("sponsorship", True, True, [r"\bsponsor", r"\bh-?1b\b", r"\bvisa\b"]),
    ("work_authorization", True, True, [r"authori[sz]ed to work", r"eligib\w* to work", r"right to work", r"work permit", r"legally (able|permitted|allowed) to work"]),
    ("relocation", False, True, [r"\brelocat"]),
    ("start_date", False, True, [r"start date", r"available to start", r"earliest (possible )?(date|start)", r"notice period", r"when can you (start|begin)"]),
    ("how_heard", False, True, [r"how did you (hear|find|learn)", r"where did you (hear|find|see)"]),
    ("referral", False, True, [r"\bwere you referred\b", r"\breferred by\b", r"\breferral\b", r"employee referr"]),
    ("previously_employed", False, True, [r"(previously|ever|formerly) (been )?(employed|worked)", r"former employee", r"worked (here|for us) before"]),
    ("previously_interviewed", False, True, [r"(previously|ever) (interviewed|applied)", r"applied (here|to us) before"]),
    ("work_arrangement", False, False, [r"\bremote\b", r"\bhybrid\b", r"on-?site", r"in[- ]office"]),
]
_BY_NAME = {name: (protected, merge) for name, protected, merge, _ in INTENTS}
_STOP = {"the", "and", "you", "your", "are", "for", "with", "this", "that", "have", "any", "our", "will", "would",
         "please", "what", "how", "why", "can", "does", "did", "not", "now", "future", "position", "role", "company"}


def normalize(question: str) -> str:
    t = re.sub(r"[^a-z0-9\s-]", " ", (question or "").lower())
    t = re.sub(r"\b(please|kindly)\b", " ", t)
    return " ".join(t.split())


def classify(question: str):
    t = " ".join((question or "").lower().split())
    if re.search(r"without\s+(\w+\s+){0,2}sponsor", t):
        return "work_authorization"          # "authorized ... without sponsorship" is the authorization question
    if re.search(r"\b(require|need)\w*\b.{0,60}\bsponsor", t):
        return "sponsorship"
    for name, _p, _m, patterns in INTENTS:
        if any(re.search(p, t) for p in patterns):
            return name
    return None


def is_protected(intent) -> bool:
    return bool(intent) and _BY_NAME.get(intent, (False, False))[0]


def _tokens(text: str) -> set:
    return {w for w in normalize(text).split() if len(w) > 2 and w not in _STOP}


def _stable_id(question: str) -> str:
    return hashlib.sha1((question or "").encode()).hexdigest()[:12]


def answer_type_for(answer: str) -> str:
    a = (answer or "").strip().lower()
    if a in ("yes", "no"):
        return "yes_no"
    if re.fullmatch(r"[\d,.$k+\- ]+", a):
        return "number"
    return "text"


def upgrade(entry) -> dict | None:
    """Any stored row -> the full shape with derived fields; None for junk."""
    if not isinstance(entry, dict):
        return None
    if "question" in entry or "answer" in entry:
        q, a = str(entry.get("question") or ""), str(entry.get("answer") or "")
    elif entry:
        q, a = next(iter(entry.items()))
        q, a = str(q or ""), str(a or "")
        entry = {}
    else:
        return None
    if not q.strip() and not a.strip():
        return None
    return {
        "id": entry.get("id") or _stable_id(q),
        "question": q,
        "aliases": [str(x) for x in entry.get("aliases") or [] if str(x).strip()],
        "answer": a,
        "answer_type": entry.get("answer_type") or answer_type_for(a),
        # rows typed on the Persona screen before this existed were the user's own words
        "user_verified": bool(entry.get("user_verified", True)),
        "last_used": entry.get("last_used"),
        "created_at": entry.get("created_at"),
        "normalized": normalize(q),
        "intent": classify(q),
    }


def stored(entry: dict) -> dict:
    return {k: entry[k] for k in ("id", "question", "aliases", "answer", "answer_type", "user_verified", "last_used", "created_at")}


def entries(bank) -> list[dict]:
    return [e for e in (upgrade(x) for x in (bank or [])) if e]


def find_answer(bank, question: str):
    """(entry, how) for the saved answer to `question`, or (None, None).

    exact wording or alias first; then another wording of the same intent; then,
    for unprotected questions only, a close word overlap.
    """
    rows = entries(bank)
    norm, intent = normalize(question), classify(question)
    for e in rows:
        if norm and (norm == e["normalized"] or norm in {normalize(a) for a in e["aliases"]}):
            if is_protected(e["intent"]) and not e["user_verified"]:
                return None, None
            return e, "exact"
    if intent and _BY_NAME.get(intent, (False, False))[1]:
        for e in rows:
            if e["intent"] == intent and e["answer"].strip() and (e["user_verified"] or not is_protected(intent)):
                return e, "intent"
    if is_protected(intent):
        return None, None
    want = _tokens(question)
    best, score = None, 0.0
    for e in rows:
        if is_protected(e["intent"]):
            continue
        for text in [e["question"], *e["aliases"]]:
            have = _tokens(text)
            j = len(want & have) / len(want | have) if want and have else 0.0
            if j > score:
                best, score = e, j
    return (best, "similar") if best is not None and score >= 0.7 else (None, None)


def save(bank, question: str, answer: str, verified: bool = True, answer_type: str | None = None):
    """(new bank, entry): merges an equivalent wording into its entry as an alias, else appends."""
    rows = entries(bank)
    norm, intent = normalize(question), classify(question)
    now = datetime.now(timezone.utc).isoformat()
    target = next((e for e in rows if norm == e["normalized"] or norm in {normalize(a) for a in e["aliases"]}), None)
    if target is None and intent and _BY_NAME.get(intent, (False, False))[1]:
        target = next((e for e in rows if e["intent"] == intent), None)
    if target is None:
        target = {"id": uuid.uuid4().hex[:12], "question": question, "aliases": [], "answer": answer,
                  "answer_type": answer_type or answer_type_for(answer), "user_verified": verified,
                  "last_used": None, "created_at": now, "normalized": norm, "intent": intent}
        rows.append(target)
    else:
        if norm != target["normalized"] and norm not in {normalize(a) for a in target["aliases"]}:
            target["aliases"].append(question)
        target["answer"] = answer
        target["answer_type"] = answer_type or answer_type_for(answer)
        target["user_verified"] = verified
    return [stored(e) for e in rows], target


def mark_used(bank, ids) -> list:
    now = datetime.now(timezone.utc).isoformat()
    ids = set(ids or [])
    return [stored({**e, "last_used": now} if e["id"] in ids else e) for e in entries(bank)]
