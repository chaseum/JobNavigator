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
    description: str = ""
    bullets: list[str] = Field(default_factory=list)


class ResumeEducation(BaseModel):
    school: str = ""
    location: str = ""
    degree: str = ""
    years: str = ""
    year: str = ""


class ResumeProject(BaseModel):
    name: str = ""
    description: str = ""
    bullets: list[str] = Field(default_factory=list)


class ResumePublication(BaseModel):
    title: str = ""
    description: str = ""
    venue: str = ""


class ResumeImport(BaseModel):
    """Resume.json_data fields consumed by résumé and profile import flows."""

    header: ResumeHeader = Field(default_factory=ResumeHeader)
    summary: str = ""
    experience: list[ResumeExperience] = Field(default_factory=list)
    skills: dict[str, list[str]] = Field(default_factory=dict)
    education: list[ResumeEducation] = Field(default_factory=list)
    projects: list[ResumeProject] = Field(default_factory=list)
    publications: list[ResumePublication] = Field(default_factory=list)
