"""Overleaf as an optional editor destination; the local LaTeX/Git version stays authoritative.

Modes (setting `overleaf_mode`): disabled · export (download .tex, PDF, or an
Overleaf-compatible ZIP from any version) · git (push an accepted version to the
project's Git remote, pull the project's state). No browser automation.

The token comes only from the OVERLEAF_GIT_TOKEN environment variable. It is sent
as a per-command HTTP header, so it never lands in .git/config, a settings row,
the database, or a log line (every git message is scrubbed before it is returned).
"""
import base64
import os
import shutil
import subprocess
from pathlib import Path

from backend.copilot.versions import export_files, generated_dir


class OverleafError(RuntimeError):
    pass


def clone_dir() -> Path:
    return generated_dir() / ".overleaf"


def _token() -> str:
    return os.getenv("OVERLEAF_GIT_TOKEN", "")


def _auth_args(remote: str) -> list[str]:
    token = _token()
    if not token or not remote.lower().startswith("http"):
        return []
    header = base64.b64encode(f"git:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {header}"]


def scrub(text: str) -> str:
    token = _token()
    if not token:
        return text
    header = base64.b64encode(f"git:{token}".encode()).decode()
    return text.replace(header, "***").replace(token, "***")


def _git(args: list[str], cwd: Path | None = None, remote: str = "") -> str:
    cmd = ["git", *_auth_args(remote), "-c", "credential.helper=", *args]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True, timeout=120)
    except FileNotFoundError:
        raise OverleafError("git is not installed")
    except subprocess.TimeoutExpired:
        raise OverleafError("git timed out")
    if proc.returncode != 0:
        raise OverleafError(scrub((proc.stderr or proc.stdout or "git failed").strip())[-800:])
    return scrub(proc.stdout)


def _has_commits(d: Path) -> bool:
    try:
        _git(["rev-parse", "--verify", "HEAD"], cwd=d)
        return True
    except OverleafError:
        return False


def ensure_clone(remote: str) -> Path:
    d = clone_dir()
    if (d / ".git").exists():
        if _git(["remote", "get-url", "origin"], cwd=d).strip() == remote:
            return d
        shutil.rmtree(d)   # remote changed: start over from the new project
    d.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", remote, str(d)], remote=remote)
    return d


def status(mode: str, remote: str) -> dict:
    d = clone_dir()
    out = {"mode": mode, "remote_configured": bool(remote), "token_configured": bool(_token()),
           "cloned": (d / ".git").exists(), "last_commit": None}
    if out["cloned"] and _has_commits(d):
        out["last_commit"] = _git(["log", "-1", "--format=%h %s (%cr)"], cwd=d).strip()
    return out


def pull(remote: str) -> dict:
    d = ensure_clone(remote)
    if _has_commits(d):
        _git(["pull", "--ff-only"], cwd=d, remote=remote)
    files = _git(["ls-files"], cwd=d).split() if _has_commits(d) else []
    return {"last_commit": _git(["log", "-1", "--format=%h %s (%cr)"], cwd=d).strip() if files else None, "files": files}


def push(version, job, remote: str) -> dict:
    if version.status != "accepted":
        raise OverleafError("only an accepted résumé is pushed to Overleaf")
    d = ensure_clone(remote)
    if _has_commits(d):
        _git(["pull", "--ff-only"], cwd=d, remote=remote)
    for name, content in export_files(version).items():
        (d / name).write_bytes(content)
    _git(["add", "-A"], cwd=d)
    if not _git(["status", "--porcelain"], cwd=d).strip():
        return {"pushed": False, "detail": "Overleaf already has this version"}
    label = f"{job.company} — {job.title}" if job is not None else "base résumé"
    _git(["-c", "user.name=JobNavigator", "-c", "user.email=jobnavigator@localhost",
          "commit", "-m", f"JobNavigator: {label} ({str(version.id)[:8]})"], cwd=d)
    _git(["push", "origin", "HEAD"], cwd=d, remote=remote)
    return {"pushed": True, "commit": _git(["log", "-1", "--format=%h %s"], cwd=d).strip()}
