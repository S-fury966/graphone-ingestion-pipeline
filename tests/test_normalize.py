"""Tests for deterministic name + domain normalization."""
import pytest

from src.resolver.normalize import normalize_name, registrable_domain

NAME_CASES = [
    ("OpenAI", "openai"),
    ("OpenAI, Inc.", "openai"),
    ("Open AI", "open ai"),            # kept distinct from "openai"
    ("Anthropic", "anthropic"),
    ("Hugging Face", "hugging face"),
    ("Café Corp", "cafe"),            # accent strip + legal suffix drop
    ("Scale AI", "scale ai"),
    ("DeepMind Ltd", "deepmind"),
    ("Acme LLC", "acme"),
    ("Foo Labs", "foo labs"),         # "labs" is NOT a legal suffix
    ("Weights & Biases", "weights biases"),
    ("Cohere Inc", "cohere"),
    ("X.AI", "x ai"),
    ("Böhm GmbH", "bohm"),
    ("  Multiple   Spaces  ", "multiple spaces"),
    ("Runway Corp.", "runway"),
    ("Mistral AI", "mistral ai"),
]


@pytest.mark.parametrize("raw,expected", NAME_CASES)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_openai_variants_collapse_but_open_ai_distinct():
    assert normalize_name("OpenAI") == normalize_name("OpenAI, Inc.") == "openai"
    assert normalize_name("Open AI") != normalize_name("OpenAI")


@pytest.mark.parametrize("url,expected", [
    ("https://openai.com", "openai.com"),
    ("http://www.OpenAI.com/", "openai.com"),
    ("https://www.foo.co.uk/path", "foo.co.uk"),
    ("sub.example.com", "example.com"),
    ("https://deep.sub.example.com", "example.com"),
    ("https://acme.com:8080/x?a=1", "acme.com"),
    ("not a url", None),
    ("", None),
    (None, None),
])
def test_registrable_domain(url, expected):
    assert registrable_domain(url) == expected
