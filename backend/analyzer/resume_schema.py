"""Typed shape for extracting an existing résumé into Resume.json_data."""

from pydantic import BaseModel, Field


class ResumeContactItem(BaseModel):
    text: str = ""
    url: str | None = None


class ResumeHeader(BaseModel):
    name: str = ""
    contact_items: list[ResumeContactItem] = Field(default_factory=list)


class ResumeExperience(BaseModel):
    company: str = ""
    title: str = ""
    location: str = ""
    date: str = ""
    start_date: str = ""
    end_date: str = ""
    employment_type: str = ""
    technologies: list[str] = Field(default_factory=list)
    description: str = ""
    bullets: list[str] = Field(default_factory=list)


class ResumeEducation(BaseModel):
    school: str = ""
    location: str = ""
    degree: str = ""
    years: str = ""
    year: str = ""
    start_date: str = ""
    graduation_date: str = ""
    major: str = ""
    minor: str = ""
    gpa: str = ""
    coursework: list[str] = Field(default_factory=list)
    honors: list[str] = Field(default_factory=list)


class ResumeResearch(BaseModel):
    organization: str = ""
    title: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    date: str = ""
    research_area: str = ""
    methods: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)
    publications: list[str] = Field(default_factory=list)


class ResumeProject(BaseModel):
    name: str = ""
    description: str = ""
    bullets: list[str] = Field(default_factory=list)
    role: str = ""
    technologies: list[str] = Field(default_factory=list)
    repository_url: str = ""
    project_url: str = ""
    start_date: str = ""
    end_date: str = ""


class ResumePublication(BaseModel):
    title: str = ""
    description: str = ""
    venue: str = ""
    date: str = ""
    authors: str = ""
    url: str = ""


class ResumeCertification(BaseModel):
    name: str = ""
    issuer: str = ""
    date: str = ""
    expires: str = ""
    credential_url: str = ""


class ResumeLink(BaseModel):
    label: str = ""
    url: str = ""


class ResumeImport(BaseModel):
    """Resume.json_data fields consumed by résumé and profile import flows."""

    header: ResumeHeader = Field(default_factory=ResumeHeader)
    summary: str = ""
    experience: list[ResumeExperience] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    education: list[ResumeEducation] = Field(default_factory=list)
    projects: list[ResumeProject] = Field(default_factory=list)
    research: list[ResumeResearch] = Field(default_factory=list)
    certifications: list[ResumeCertification] = Field(default_factory=list)
    publications: list[ResumePublication] = Field(default_factory=list)
    links: list[ResumeLink] = Field(default_factory=list)
