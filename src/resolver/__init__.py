"""Entity resolution: name/domain normalization + (later) canonicalization."""
from .normalize import normalize_name, registrable_domain, strip_accents

__all__ = ["normalize_name", "registrable_domain", "strip_accents"]
