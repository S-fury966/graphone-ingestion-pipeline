"""Pydantic v2 record schemas for the ingestion pipeline."""
from .base import BaseRecord, SourceInfo, flatten_dict
from .enums import DateConfidence, PricingModel
from .job import Job, JobContent
from .news import News, NewsContent
from .product import Product, ProductContent
from .research_paper import ResearchPaper, ResearchPaperContent
from .startup import Startup, StartupContent, StartupData

__all__ = [
    "BaseRecord",
    "SourceInfo",
    "flatten_dict",
    "PricingModel",
    "DateConfidence",
    "Startup",
    "StartupContent",
    "StartupData",
    "Product",
    "ProductContent",
    "ResearchPaper",
    "ResearchPaperContent",
    "Job",
    "JobContent",
    "News",
    "NewsContent",
]
