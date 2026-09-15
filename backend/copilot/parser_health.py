"""Parser Health: can plain text extraction read the compiled PDF back correctly?

Not an ATS score. No one outside a vendor knows how any employer ranks résumés;
what can be checked is whether the words survive extraction in a sensible order,
which is the precondition for any parser at all.
"""
import io
import re
import unicodedata

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}


def normalize(text: str) -> str:
    t = "".join(_LIGATURES.get(c, c) for c in unicodedata.normalize("NFKC", text or ""))
    t = re.sub(r"-\n(?=[a-z])", "", t)          # hyphenated line breaks
    return re.sub(r"\s+", " ", t).strip().lower()


def extract_text(pdf: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        return "\n".join(page.extract_text() or "" for page in doc.pages)


def _found(needle: str, hay: str) -> bool:
    return bool(needle) and normalize(needle) in hay


def check(raw_text: str, resume: dict) -> dict:
    """{"score": 0-100, "checks": [{name, ok, detail}]} for text extracted from the PDF of `resume`."""
    hay = normalize(raw_text)
    sections = resume.get("sections") or []
    entries = [(s, e) for s in sections for e in s.get("entries") or []]
    checks = []

    def add(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    name = (resume.get("header") or {}).get("name") or ""
    add("Name extracted", _found(name, hay), "" if _found(name, hay) else f"'{name}' not found in the text")

    missing = [s["title"] for s in sections if not _found(s["title"], hay)]
    add("Section headings extracted", not missing, f"missing: {', '.join(missing)}" if missing else "")

    positions = [hay.find(normalize(s["title"])) for s in sections if _found(s["title"], hay)]
    add("Sections in reading order", positions == sorted(positions), "" if positions == sorted(positions) else "headings come out of order")

    for label, sec_ids, field in (("Company names extracted", ("experience",), "heading"),
                                  ("Job titles extracted", ("experience",), "subheading"),
                                  ("Schools extracted", ("education",), "heading")):
        wanted = [e.get(field) for s, e in entries if s.get("id") in sec_ids and e.get(field)]
        lost = [w for w in wanted if not _found(w, hay)]
        add(label, not lost, f"missing: {', '.join(lost)}" if lost else ("none on this résumé" if not wanted else ""))

    skills = [i for s in sections for line in s.get("lines") or [] for i in line.get("items") or []]
    lost = [k for k in skills if not _found(k, hay)]
    add("Skills extracted", len(lost) <= len(skills) * 0.1, f"missing: {', '.join(lost[:8])}" if lost else "")

    disorder = []
    for _, e in entries:
        starts = [hay.find(normalize(" ".join(b["text"].split()[:5]))) for b in e.get("bullets") or []]
        if -1 in starts or starts != sorted(starts):
            disorder.append(e.get("heading") or "?")
    add("Bullet order preserved", not disorder, f"out of order or unreadable under: {', '.join(disorder)}" if disorder else "")

    garbled = "�" in raw_text or "(cid:" in raw_text
    add("No garbled characters", not garbled, "replacement glyphs or unmapped font codes in the text" if garbled else "")

    passed = sum(c["ok"] for c in checks)
    return {"score": round(passed / len(checks) * 100), "checks": checks}
