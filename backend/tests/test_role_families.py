"""Role families: classification is deterministic, and a family base is a selection, not a rewrite."""
import pytest

from backend.copilot import role_families as RF
from backend.copilot import resume_pipeline as P


def _analysis(title, technologies=(), keywords=(), requirements=()):
    return {"title": title, "technologies": list(technologies), "keywords": list(keywords),
            "requirements": [{"id": f"r{i}", "text": t} for i, t in enumerate(requirements, 1)]}


@pytest.mark.parametrize("title,expected", [
    ("Software Engineer", "software_engineering"),
    ("Backend Engineer, Payments", "software_engineering"),
    ("Site Reliability Engineer", "software_engineering"),
    ("Technical Product Manager", "product"),
    ("Product Manager, Growth", "product"),
    ("Data Scientist", "data_ml"),
    ("Machine Learning Engineer", "data_ml"),
    ("Applied Scientist, NLP", "data_ml"),
])
def test_titles_classify_to_the_expected_family(title, expected):
    assert RF.classify(_analysis(title))["family"] == expected


def test_classification_explains_itself_and_reports_confidence():
    out = RF.classify(_analysis("Machine Learning Engineer", technologies=["PyTorch", "Python"]))
    assert out["family"] == "data_ml" and out["label"] == "Data / Machine Learning"
    assert 0 < out["confidence"] <= 1
    assert "machine learning" in out["reason"]


def test_an_unrecognisable_title_falls_back_and_says_so():
    out = RF.classify(_analysis("Chief of Staff"))
    assert out["family"] in {f["id"] for f in RF.families()}
    assert out["confidence"] == 0 and "default" in out["reason"]


def test_a_user_defined_family_joins_the_builtins(test_db):
    from backend.models.db import Setting
    test_db.add(Setting(key=RF.SETTING_KEY, value='[{"id": "security", "label": "Security Engineering", '
                                                  '"title": ["security engineer", "appsec"], "evidence": ["threat", "pentest"]}]'))
    test_db.commit()
    ids = [f["id"] for f in RF.families(test_db)]
    assert "security" in ids and {"software_engineering", "product", "data_ml"} <= set(ids)
    assert RF.classify(_analysis("Security Engineer"), test_db)["family"] == "security"


def test_a_malformed_custom_family_is_ignored_not_fatal(test_db):
    from backend.models.db import Setting
    test_db.add(Setting(key=RF.SETTING_KEY, value="not json"))
    test_db.commit()
    assert [f["id"] for f in RF.families(test_db)] == [f["id"] for f in RF.BUILTIN]


def test_relevance_counts_only_terms_the_text_actually_contains():
    family = RF.get("data_ml")
    assert RF.relevance(family, "Trained a classification model in PyTorch") > 0
    assert RF.relevance(family, "Answered the phone politely") == 0


# ── the family base is a view of one truth ───────────────────────────────────

class _Fact:
    def __init__(self, id, kind, data, parent_id=None):
        self.id, self.kind, self.data, self.parent_id = id, kind, data, parent_id


def _index():
    facts = [
        _Fact(1, "experience", {"employer": "Acme", "title": "Engineer", "start_date": "2024", "end_date": "2025"}),
        _Fact(2, "achievement", {"text": "Wrote the stakeholder roadmap and ran the launch review"}, parent_id=1),
        _Fact(3, "achievement", {"text": "Built a FastAPI service backed by PostgreSQL"}, parent_id=1),
        _Fact(4, "achievement", {"text": "Trained a classification model in PyTorch"}, parent_id=1),
        _Fact(5, "skill", {"name": "PyTorch", "category": "Libraries"}),
        _Fact(6, "skill", {"name": "Roadmapping", "category": "Product"}),
        _Fact(7, "project", {"name": "Ledgerly", "description": "A FastAPI and React budgeting app", "end_date": "2024"}),
        _Fact(8, "project", {"name": "Churn model", "description": "Logistic regression on a 5k dataset", "end_date": "2023"}),
    ]
    return P.FactIndex(facts)


def _bullets(resume, fact_id="experience_1"):
    entry = next(e for s in resume["sections"] for e in s.get("entries") or [] if e["fact_id"] == fact_id)
    return [b["text"] for b in entry["bullets"]]


def test_each_family_leads_with_the_evidence_it_supports():
    ix = _index()
    swe = P.build_base(ix, None, family=RF.get("software_engineering"))
    pm = P.build_base(ix, None, family=RF.get("product"))
    ml = P.build_base(ix, None, family=RF.get("data_ml"))

    assert "FastAPI" in _bullets(swe)[0]
    assert "roadmap" in _bullets(pm)[0]
    assert "PyTorch" in _bullets(ml)[0]
    assert swe["role_family"] == "software_engineering"


def test_a_family_base_selects_and_orders_but_never_invents_or_drops_the_truth():
    ix = _index()
    plain = P.build_base(ix, None)
    for fid in ("software_engineering", "product", "data_ml"):
        family = P.build_base(ix, None, family=RF.get(fid))
        assert sorted(_bullets(family)) == sorted(_bullets(plain)), "same facts, different order"
        assert {s["id"] for s in family["sections"]} == {s["id"] for s in plain["sections"]}


def test_a_family_base_reorders_projects_and_skills_by_the_same_evidence():
    ix = _index()
    ml = P.build_base(ix, None, family=RF.get("data_ml"))
    projects = [e["heading"] for s in ml["sections"] if s["id"] == "projects" for e in s["entries"]]
    assert projects[0] == "Churn model", "the older but more relevant project leads for Data/ML"

    skills = [i for s in ml["sections"] if s["id"] == "skills" for line in s["lines"] for i in line["items"]]
    assert skills.index("PyTorch") < skills.index("Roadmapping")


def test_a_tailored_draft_inherits_its_bases_project_and_skill_selection():
    ix = _index()
    base = P.build_base(ix, None, family=RF.get("data_ml"))
    assert P.section_refs(base, "projects")[0] == "project_8"
    assert P.section_refs(base, "skills")[0] == "skill_5"
