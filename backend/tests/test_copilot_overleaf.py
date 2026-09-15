"""Overleaf: push accepted versions to a Git remote without ever persisting the token; pull its state."""
import shutil
import subprocess
import uuid

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _accepted(db, status="accepted"):
    from backend.models.db import Job, ResumeVersion, utcnow
    job = Job(external_id=str(uuid.uuid4()), company="Acme", title="Engineer", url="https://acme.test/1")
    db.add(job)
    db.flush()
    v = ResumeVersion(job_id=job.id, kind="tailored", status="draft", resume_json={"header": {"name": "Ada"}, "sections": []},
                      latex="\\documentclass{article}\\begin{document}Ada\\end{document}", pdf=b"%PDF-1.4 x")
    db.add(v)
    db.commit()
    if status == "accepted":
        v.status, v.accepted_at = "accepted", utcnow()
        db.commit()
    return job, v


def _settings(db, **kv):
    from backend.models.db import Setting
    for k, val in kv.items():
        db.add(Setting(key=k, value=val))
    db.commit()


def test_push_and_pull_with_a_git_remote(api_client, test_db, tmp_path, monkeypatch):
    remote = tmp_path / "overleaf.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    monkeypatch.setenv("GENERATED_DIR", str(tmp_path / "generated"))
    monkeypatch.setenv("OVERLEAF_GIT_TOKEN", "tok-SECRET-123")
    _settings(test_db, dashboard_api_key="", overleaf_mode="git", overleaf_git_remote=str(remote))
    job, draft = _accepted(test_db, status="draft")
    assert api_client.post(f"/api/resume-versions/{draft.id}/overleaf").status_code == 409

    _, v = _accepted(test_db)
    r = api_client.post(f"/api/resume-versions/{v.id}/overleaf")
    assert r.status_code == 200 and r.json()["pushed"] is True, r.text
    tex = subprocess.run(["git", "--git-dir", str(remote), "show", "HEAD:resume.tex"], capture_output=True, text=True, check=True).stdout
    assert "Ada" in tex
    files = subprocess.run(["git", "--git-dir", str(remote), "ls-tree", "--name-only", "HEAD"], capture_output=True, text=True).stdout.split()
    assert {"resume.tex", "macros.tex", "resume.pdf"} <= set(files)

    again = api_client.post(f"/api/resume-versions/{v.id}/overleaf").json()
    assert again["pushed"] is False

    pulled = api_client.post("/api/copilot/overleaf/pull").json()
    assert "resume.tex" in pulled["files"] and pulled["last_commit"]
    status = api_client.get("/api/copilot/overleaf").json()
    assert status["cloned"] and status["token_configured"] and status["last_commit"]

    config = (tmp_path / "generated" / ".overleaf" / ".git" / "config").read_text()
    assert "tok-SECRET-123" not in config


def test_git_mode_is_required(api_client, test_db, tmp_path, monkeypatch):
    monkeypatch.setenv("GENERATED_DIR", str(tmp_path))
    _settings(test_db, dashboard_api_key="", overleaf_mode="export")
    _, v = _accepted(test_db)
    assert api_client.post(f"/api/resume-versions/{v.id}/overleaf").status_code == 400
    assert api_client.post("/api/copilot/overleaf/pull").status_code == 400
    assert api_client.get(f"/api/resume-versions/{v.id}/export.zip").content[:2] == b"PK"


def test_token_is_scrubbed_from_git_messages(monkeypatch):
    import base64
    from backend.copilot import overleaf
    monkeypatch.setenv("OVERLEAF_GIT_TOKEN", "abc123token")
    header = base64.b64encode(b"git:abc123token").decode()
    assert overleaf.scrub(f"fatal: auth abc123token {header}") == "fatal: auth *** ***"
    assert overleaf._auth_args("https://git.overleaf.com/xyz")[1].startswith("http.extraHeader=Authorization: Basic ")
    assert overleaf._auth_args("/local/path.git") == []


def test_dashboard_summarizes_the_workflow(api_client, test_db):
    from backend.models.db import Application, CandidateFact, Job, JobAnalysisRecord
    _settings(test_db, dashboard_api_key="")
    job = Job(external_id="d1", company="Acme", title="Engineer", url="https://acme.test/2", saved=True)
    test_db.add(job)
    test_db.flush()
    test_db.add_all([JobAnalysisRecord(job_id=job.id, analysis={"requirements": []}, evidence=[], match={"score": 81}, profile_version="old"),
                     CandidateFact(kind="project", data={"name": "x"}, verified=False),
                     Application(job_id=job.id, status="ready_to_apply")])
    test_db.commit()
    d = api_client.get("/api/copilot/dashboard").json()
    assert d["counts"]["analyzed"] == 1 and d["counts"]["saved_jobs"] == 1
    assert d["generate"][0]["score"] == 81 and d["apply"][0]["title"] == "Engineer"
    assert d["profile"]["unverified"] == 1 and d["stale_matches"] == 1
