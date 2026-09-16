"""One wedged board must never strand a keyword search.

python-jobspy 1.1.82 can hang paginating Indeed. Each board therefore runs in its
own subprocess behind subprocess.run(timeout=), which kills the child; these tests
pin the contract that a killed/failed board costs only itself.
"""
import io
import json
import subprocess

import pytest

from backend.scraper.sources import jobspy as J


def test_board_key_matches_the_site_spelling_rows_come_back_with():
    assert J.board_key("ziprecruiter") == "zip_recruiter"
    assert J.board_key("ZipRecruiter") == "zip_recruiter"
    assert J.board_key("indeed") == "indeed"
    assert J.board_key("somewhere_new") == "somewhere_new"


def test_a_hung_board_is_killed_and_reported(monkeypatch):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
    monkeypatch.setattr(J.subprocess, "run", fake_run)
    rows, error = J._scrape_board_sync("indeed", {}, 30)
    assert rows == [] and error == "timed out after 30s"


def test_a_crashed_worker_reports_its_stderr_not_a_silent_empty_board(monkeypatch):
    monkeypatch.setattr(J.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, b"", b"Traceback\nRuntimeError: boom"))
    rows, error = J._scrape_board_sync("google", {}, 30)
    assert rows == [] and "boom" in error


def test_one_stuck_board_does_not_lose_the_others(monkeypatch):
    def fake_board(board, kwargs, timeout):
        if board == "indeed":
            return [], f"timed out after {int(timeout)}s"
        if board == "ziprecruiter":
            return [], "403"
        return [{"site": board, "title": f"Engineer ({board})"}], None
    monkeypatch.setattr(J, "_scrape_board_sync", fake_board)

    rows, errors = J.scrape_boards(["linkedin", "indeed", "ziprecruiter", "google"], {"search_term": "x"}, 30)
    assert sorted(r["site"] for r in rows) == ["google", "linkedin"]
    assert errors == {"indeed": "timed out after 30s", "zip_recruiter": "403"}


def test_every_board_runs_on_its_own_and_gets_only_its_own_site_name(monkeypatch):
    seen = []

    def fake_board(board, kwargs, timeout):
        seen.append((board, dict(kwargs)))
        return [], None
    monkeypatch.setattr(J, "_scrape_board_sync", fake_board)

    J.scrape_boards(["linkedin", "indeed"], {"search_term": "swe", "results_wanted": 20}, 5)
    assert sorted(b for b, _ in seen) == ["indeed", "linkedin"]
    # site_name is the worker's business: the parent never asks for a multi-board call
    assert all("site_name" not in kw for _, kw in seen)


class _FakeFrame:
    """Just the DataFrame surface `_worker_main` touches (pandas import segfaults in this venv)."""

    def __init__(self, records):
        self._records = records
        self.empty = not records

    def to_dict(self, orient):
        assert orient == "records"
        return list(self._records)


def test_worker_returns_cleaned_rows_and_the_board_error_jobspy_only_logged(monkeypatch, capsys):
    def fake_scrape_jobs(**kwargs):
        assert kwargs["site_name"] == ["indeed"]
        import logging
        logging.getLogger("JobSpy:Indeed").warning("Indeed response status code 403")
        return _FakeFrame([{"site": "indeed", "title": "SWE", "company": "Acme",
                            "job_url": "https://x/1", "min_amount": float("nan")}])

    monkeypatch.setitem(__import__("sys").modules, "jobspy",
                        type("m", (), {"scrape_jobs": staticmethod(fake_scrape_jobs)}))
    monkeypatch.setattr(J.sys, "stdin", io.StringIO(json.dumps({"board": "indeed", "kwargs": {"search_term": "swe"}})))

    assert J._worker_main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "403", "a board that 403s must not look like a board that found nothing"
    assert out["rows"] == [{"site": "indeed", "title": "SWE", "company": "Acme",
                            "job_url": "https://x/1", "min_amount": None}]


def test_worker_reports_an_exception_instead_of_dying_silently(monkeypatch, capsys):
    def boom(**kwargs):
        raise RuntimeError("pagination never ended")
    monkeypatch.setitem(__import__("sys").modules, "jobspy", type("m", (), {"scrape_jobs": staticmethod(boom)}))
    monkeypatch.setattr(J.sys, "stdin", io.StringIO(json.dumps({"board": "linkedin", "kwargs": {}})))

    assert J._worker_main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows"] == [] and "pagination never ended" in out["error"]


@pytest.mark.parametrize("stored,expected", [("", J.DEFAULT_BOARD_TIMEOUT), ("45", 45.0),
                                             ("0", J.DEFAULT_BOARD_TIMEOUT), ("nonsense", J.DEFAULT_BOARD_TIMEOUT)])
def test_board_timeout_setting(monkeypatch, stored, expected):
    monkeypatch.setattr(J, "get_setting_value", lambda db, key, default="": stored)
    assert J.board_timeout(None) == expected
