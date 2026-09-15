"""Structured résumé JSON -> LaTeX (deterministic template) -> PDF via the local TeX toolchain.

The LLM never writes LaTeX. It fills bullet text in structured JSON; this module
escapes every value and renders a fixed template, so formatting, margins, fonts,
section order and links are the template's, not the model's.
"""
import asyncio
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "resume" / "templates"

_ESCAPES = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "<": r"\textless{}", ">": r"\textgreater{}", "|": r"\textbar{}",
    "–": "--", "—": "---", "‘": "`", "’": "'", "“": "``", "”": "''",
    "•": r"\textbullet{}", " ": "~",
}


class Raw(str):
    """Already-safe LaTeX (an escaped URL); `finalize` leaves it alone."""


def escape(value) -> str:
    return "".join(_ESCAPES.get(ch, ch) for ch in str(value))


def _url(value) -> Raw:
    # inside \href only these break the argument; backslash and braces are dropped, never a valid URL
    s = "".join(ch for ch in str(value or "") if ch not in "\\{}")
    return Raw(s.replace("%", r"\%").replace("#", r"\#"))


def _finalize(v):
    if v is None:
        return ""
    return v if isinstance(v, Raw) else escape(v)


def template_names() -> list[str]:
    return sorted(p.name for p in TEMPLATES_DIR.iterdir() if (p / "resume.tex.j2").is_file()) if TEMPLATES_DIR.is_dir() else []


def template_dir(name: str) -> Path:
    if name not in template_names():
        raise ValueError(f"unknown résumé template {name!r}")
    return TEMPLATES_DIR / name


def template_version(name: str) -> str:
    """Hash of every file in the template folder, recorded on each résumé version."""
    h = hashlib.sha256()
    for p in sorted(template_dir(name).rglob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


def render(resume: dict, template: str = "default") -> str:
    env = Environment(
        loader=FileSystemLoader(str(template_dir(template))),
        block_start_string="((*", block_end_string="*))",
        variable_start_string="(((", variable_end_string=")))",
        comment_start_string="((=", comment_end_string="=))",
        finalize=_finalize, undefined=StrictUndefined,
        trim_blocks=True, lstrip_blocks=True, autoescape=False,
    )
    env.filters["url"] = _url
    return env.get_template("resume.tex.j2").render(**resume)


def compiler() -> str | None:
    return shutil.which("pdflatex")


def _errors_from_log(log: str) -> str:
    """The `! ...` error lines with a little context; the whole tail if there are none."""
    lines = log.splitlines()
    picked = []
    for i, line in enumerate(lines):
        if line.startswith("!"):
            picked.extend(lines[i:i + 4])
    return "\n".join(picked) if picked else "\n".join(lines[-25:])


async def compile_pdf(tex: str, template: str = "default", timeout: float = 90) -> dict:
    """{"ok", "pdf", "pages", "log"}; a failure carries the TeX error, never a half-built PDF."""
    exe = compiler()
    if not exe:
        return {"ok": False, "pdf": None, "pages": 0,
                "log": "pdflatex not found. Install a TeX distribution (TeX Live or MiKTeX), or use the Docker image which ships one."}
    with tempfile.TemporaryDirectory(prefix="jn-latex-") as work:
        workdir = Path(work)
        for f in template_dir(template).iterdir():
            if f.is_file() and f.suffix != ".j2":
                shutil.copy(f, workdir / f.name)
        (workdir / "resume.tex").write_text(tex, encoding="utf-8")
        try:
            proc = await asyncio.to_thread(
                subprocess.run, [exe, "-interaction=nonstopmode", "-halt-on-error", "resume.tex"],
                cwd=str(workdir), stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"ok": False, "pdf": None, "pages": 0, "log": f"pdflatex timed out after {int(timeout)}s"}
        log_file = workdir / "resume.log"
        log = log_file.read_text(encoding="utf-8", errors="replace") if log_file.exists() else proc.stdout.decode(errors="replace")
        pdf_path = workdir / "resume.pdf"
        if proc.returncode != 0 or not pdf_path.exists():
            return {"ok": False, "pdf": None, "pages": 0, "log": _errors_from_log(log)}
        pdf = pdf_path.read_bytes()
    import io
    import pdfplumber
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        pages = len(doc.pages)
    return {"ok": True, "pdf": pdf, "pages": pages, "log": ""}
