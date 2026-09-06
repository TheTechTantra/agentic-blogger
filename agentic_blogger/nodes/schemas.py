"""Pydantic schemas used with .with_structured_output() by the LLM-backed nodes."""

from typing import Optional

from pydantic import BaseModel, Field


class SourceItem(BaseModel):
    url: str
    title: str = ""
    snippet: str = ""


class SourceList(BaseModel):
    sources: list[SourceItem] = Field(default_factory=list)


class OutlineSection(BaseModel):
    heading: str
    key_points: list[str] = Field(default_factory=list)


class Outline(BaseModel):
    title_options: list[str]
    sections: list[OutlineSection]


class FactCheckFinding(BaseModel):
    claim: str
    verdict: str = Field(description="one of: supported, unsupported, contradicted")
    confidence: float
    source_url: Optional[str] = None
    fix: Optional[str] = None


class FactCheckReport(BaseModel):
    findings: list[FactCheckFinding] = Field(default_factory=list)


class SeoMeta(BaseModel):
    title: str
    meta_description: str
    labels: list[str] = Field(default_factory=list)
    slug: str
