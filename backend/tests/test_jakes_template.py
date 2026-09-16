"""The vendored Jake's Resume template must render, compile and survive text extraction.

Not a pixel snapshot: what matters is that a realistic résumé reaches a PDF whose
text a parser can still read back. Skipped where no TeX toolchain is installed
(the backend image ships one; see Dockerfile.backend).
"""
import pytest

from backend.copilot import latex, parser_health

RESUME = {
    "template": "jakes",
    "header": {
        "name": "Dana Okafor",
        "contact": [
            {"text": "dana@example.edu", "url": "mailto:dana@example.edu"},
            {"text": "512-555-0142", "url": ""},
            {"text": "Austin, TX", "url": ""},
            {"text": "linkedin.com/in/danaokafor", "url": "https://linkedin.com/in/danaokafor"},
        ],
    },
    "sections": [
        {"id": "education", "title": "Education", "entries": [{
            "fact_id": "education_1", "heading": "University of Texas at Austin", "subheading": "B.S. Computer Science",
            "location": "Austin, TX", "date": "May 2027",
            "bullets": [{"id": "education_1:1", "text": "GPA: 3.8; Honors: Dean's List", "source_fact_ids": ["education_1"]}],
        }]},
        {"id": "experience", "title": "Experience", "entries": [{
            "fact_id": "internship_2", "heading": "CED Engineering & Design", "subheading": "Software Engineering Intern",
            "location": "Houston, TX", "date": "May 2025 – Aug 2025",
            "bullets": [
                {"id": "internship_2:1", "text": "Built a FastAPI service backed by PostgreSQL that cut report turnaround from 40 minutes to 6",
                 "source_fact_ids": ["achievement_9"]},
                {"id": "internship_2:2", "text": "Wrote 100% of the ingest pipeline's tests (C++ & Python) — 30% fewer escaped defects",
                 "source_fact_ids": ["achievement_10"]},
            ],
        }]},
        {"id": "research", "title": "Research", "entries": [{
            "fact_id": "research_3", "heading": "UT Robotics Lab", "subheading": "Undergraduate Researcher",
            "location": "Austin, TX", "date": "Jan 2025 – Present",
            "bullets": [{"id": "research_3:1", "text": "Evaluated grasp-planning models on a 5,000-sample dataset", "source_fact_ids": ["research_3"]}],
        }]},
        {"id": "projects", "title": "Projects", "entries": [{
            "fact_id": "project_4", "heading": "Ledgerly", "subheading": "Python, React, Docker", "location": "", "date": "2024",
            "bullets": [{"id": "project_4:1", "text": "Open-source budgeting app with 1,200+ downloads", "source_fact_ids": ["project_4"]}],
        }]},
        {"id": "skills", "title": "Skills", "lines": [
            {"label": "Languages", "items": ["Python", "C++", "SQL (Postgres)", "JavaScript"], "source_fact_ids": ["skill_5"]},
            {"label": "Frameworks", "items": ["FastAPI", "React", "PyTorch"], "source_fact_ids": ["skill_6"]},
        ]},
        {"id": "certifications", "title": "Certifications", "entries": [{
            "fact_id": "certification_7", "heading": "AWS Certified Cloud Practitioner", "subheading": "Amazon Web Services",
            "location": "", "date": "2025", "bullets": [],
        }]},
    ],
}


def test_jakes_is_the_default_template():
    assert latex.DEFAULT_TEMPLATE == "jakes"
    assert "jakes" in latex.template_names()


def test_render_uses_jakes_macros():
    tex = latex.render(RESUME, "jakes")
    for macro in ("\\input{macros}", "\\resumeSubHeadingListStart", "\\resumeSubheading",
                  "\\resumeProjectHeading", "\\resumeItemListStart", "\\resumeItem{"):
        assert macro in tex, macro
    # headings are copied from facts and escaped, never emitted by a model
    assert "CED Engineering \\& Design" in tex
    assert "\\href{mailto:dana@example.edu}{\\underline{dana@example.edu}}" in tex


@pytest.mark.asyncio
async def test_jakes_compiles_and_survives_text_extraction():
    if latex.compiler() is None:
        pytest.skip("pdflatex not installed")
    result = await latex.compile_pdf(latex.render(RESUME, "jakes"), "jakes")
    assert result["ok"], result["log"]
    assert result["pages"] >= 1

    text = parser_health.normalize(parser_health.extract_text(result["pdf"]))
    for needle in ("dana okafor", "dana@example.edu", "linkedin.com/in/danaokafor",
                   "education", "experience", "research", "projects", "skills", "certifications",
                   "university of texas at austin", "b.s. computer science", "may 2027",
                   "ced engineering & design", "software engineering intern", "may 2025", "aug 2025",
                   "fastapi service backed by postgresql", "ledgerly",
                   "languages", "python", "c++", "sql (postgres)",
                   "frameworks", "pytorch", "aws certified cloud practitioner"):
        assert needle in text, f"{needle!r} did not survive PDF text extraction"

    health = parser_health.check(parser_health.extract_text(result["pdf"]), RESUME)
    failed = [c["name"] for c in health["checks"] if not c["ok"]]
    assert not failed, failed
