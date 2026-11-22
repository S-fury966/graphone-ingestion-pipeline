"""Enumerations used across record schemas."""
from enum import Enum


class PricingModel(str, Enum):
    FREE = "FREE"
    FREEMIUM = "FREEMIUM"
    PAID = "PAID"
    ENTERPRISE = "ENTERPRISE"


class DateConfidence(str, Enum):
    """How a record's date was determined."""
    EXPLICIT = "EXPLICIT"    # parsed from a real date field / meta tag
    RELATIVE = "RELATIVE"    # derived from "2 hours ago" style text
    HEURISTIC = "HEURISTIC"  # inferred via "new since last run"
