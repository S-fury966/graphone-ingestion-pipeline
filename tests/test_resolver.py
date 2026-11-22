"""Entity resolver tests (Step 13): corpus precision/recall, per-layer units,
threshold + margin edges, and determinism."""
from pathlib import Path

import yaml
import pytest

from src.resolver.normalize import (clean_display_name, compact_key,
                                     normalize_name)
from src.resolver.resolver import (DOMAIN, EXACT, FUZZY, NEW_ENTITY, PRODUCT,
                                    STARTUP, TABLE, Resolution, Resolver,
                                    TableRow, extract_domain)

FIX = Path(__file__).parent / "fixtures" / "entity_eval.yaml"
SEED = "config/seed_entities.yaml"
CFG = "config/resolver.yaml"


@pytest.fixture(scope="module")
def resolver():
    return Resolver(seed_path=SEED, config_path=CFG)


def _cases():
    data = yaml.safe_load(FIX.read_text(encoding="utf-8"))
    return data["cases"]


# ============================ corpus precision / recall ============================
def test_corpus_precision_recall(resolver):
    cases = _cases()
    positives = [c for c in cases if c["kind"] == "positive"]
    negatives = [c for c in cases if c["kind"] == "negative"]
    assert len(positives) >= 40, "need >= 40 positive cases"
    assert len(negatives) >= 25, "need >= 25 negative cases"

    correct = 0
    pos_misses = []
    for c in positives:
        res = resolver.resolve(c["raw"], c["type"], startup=c.get("startup"))
        if res.canonical == c["expect"]:
            correct += 1
        else:
            pos_misses.append((c["raw"], c["expect"], res.canonical, res.method))
    recall = correct / len(positives)

    wrong_merges = []
    for c in negatives:
        res = resolver.resolve(c["raw"], c["type"], startup=c.get("startup"))
        if c["expect"] == "NEW_ENTITY":
            merged = res.method != NEW_ENTITY
        else:
            merged = res.canonical != c["expect"]
        if merged:
            wrong_merges.append((c["raw"], res.canonical, res.method, res.score))
    precision = 1.0 - len(wrong_merges) / len(negatives)

    print(f"\nrecall={recall:.3f} ({correct}/{len(positives)})  "
          f"precision={precision:.3f} (wrong merges={len(wrong_merges)}/{len(negatives)})")
    if pos_misses:
        print("positive misses:", pos_misses)
    if wrong_merges:
        print("WRONG MERGES:", wrong_merges)

    assert precision == 1.0, f"precision must be 1.0; wrong merges: {wrong_merges}"
    assert recall >= 0.90, f"recall must be >= 0.90; misses: {pos_misses}"


# ============================ per-layer unit tests ============================
def test_layer1_exact_compact_key(resolver):
    for raw in ["OpenAI", "OpenAI, Inc.", "Open AI", "open-ai", "OPENAI"]:
        res = resolver.resolve(raw, STARTUP)
        assert res.canonical == "OpenAI" and res.method == EXACT and res.score == 1.0


def test_layer2_domain_match(resolver):
    res = resolver.resolve("https://openai.com/about", STARTUP)
    assert res.canonical == "OpenAI" and res.method == DOMAIN
    res2 = resolver.resolve("www.anthropic.com", STARTUP)
    assert res2.canonical == "Anthropic" and res2.method == DOMAIN
    # a plain name with no domain must NOT trigger domain matching
    assert resolver.resolve("Scale AI", STARTUP).method == EXACT


def test_layer3_table_match():
    rows = [TableRow(canonical="YC Rocket Labs", domain="ycrocket.io")]
    r = Resolver(seed_path=SEED, config_path=CFG, startup_rows=rows)
    by_name = r.resolve("yc rocket labs", STARTUP)
    assert by_name.canonical == "YC Rocket Labs" and by_name.method == TABLE
    by_domain = r.resolve("https://ycrocket.io/jobs", STARTUP)
    assert by_domain.canonical == "YC Rocket Labs" and by_domain.method == TABLE


def test_layer4_fuzzy_typo(resolver):
    res = resolver.resolve("Anthropc", STARTUP)
    assert res.canonical == "Anthropic" and res.method == FUZZY
    assert res.score >= 0.93


def test_layer5_new_entity_cleans_suffix(resolver):
    res = resolver.resolve("Totally Unknown Startup Inc", STARTUP)
    assert res.method == NEW_ENTITY
    assert res.canonical == "Totally Unknown Startup"   # suffix stripped, casing kept


# ============================ guard / edge cases ============================
def test_short_names_never_fuzzy_merge(resolver):
    # "Grok" (4 chars) must not merge into "Groq"
    assert resolver.resolve("Grok", STARTUP).method == NEW_ENTITY


def test_do_not_merge_pair_blocks(resolver):
    assert resolver.resolve("Cohere Health", STARTUP).method == NEW_ENTITY
    assert resolver.resolve("Scale Venture Partners", STARTUP).method == NEW_ENTITY


def test_generic_word_not_merged(resolver):
    assert resolver.resolve("Character", STARTUP).method == NEW_ENTITY
    assert resolver.resolve("Scale", STARTUP).method == NEW_ENTITY


def test_min_score_threshold(tmp_path):
    seed = tmp_path / "seed.yaml"
    seed.write_text(yaml.safe_dump({
        "seed_entities": [{"canonical": "Brightwave", "domain": "brightwave.com",
                           "aliases": []}],
        "do_not_merge": [],
    }), encoding="utf-8")
    r = Resolver(seed_path=str(seed), config_path=CFG)
    # "Brightwave" -> exact; a distant variant falls below min_score -> NEW
    assert r.resolve("Brightwave", STARTUP).method == EXACT
    assert r.resolve("Brightwiz", STARTUP).method == NEW_ENTITY


def test_second_best_margin_rejects(tmp_path):
    # Two canonicals equidistant from the query -> ambiguous -> NEW_ENTITY.
    seed = tmp_path / "seed.yaml"
    seed.write_text(yaml.safe_dump({
        "seed_entities": [
            {"canonical": "Alfacorp", "aliases": []},
            {"canonical": "Alfacore", "aliases": []},
        ],
        "do_not_merge": [],
    }), encoding="utf-8")
    r = Resolver(seed_path=str(seed), config_path=CFG)
    res = r.resolve("Alfacor", STARTUP)   # ratio ties against both
    assert res.method == NEW_ENTITY, res


# ============================ product scoping ============================
def test_product_scoped_to_startup(resolver):
    assert resolver.resolve("Chat GPT", PRODUCT, startup="OpenAI").canonical == "ChatGPT"
    # Claude under the WRONG startup must not resolve to Anthropic's Claude
    assert resolver.resolve("Claude", PRODUCT, startup="OpenAI").method == NEW_ENTITY


def test_same_product_name_two_startups_stays_separate():
    startups = [TableRow(canonical="Alpha"), TableRow(canonical="Beta")]
    products = [TableRow(canonical="Copilot", startup="Alpha"),
                TableRow(canonical="Copilot", startup="Beta")]
    r = Resolver(seed_path=SEED, config_path=CFG,
                 startup_rows=startups, product_rows=products)
    assert r.resolve("Copilot", PRODUCT, startup="Alpha").canonical == "Copilot"
    assert r.resolve("Copilot", PRODUCT, startup="Beta").method in (EXACT, TABLE)
    # unscoped: ambiguous owner -> NEW_ENTITY (never guess which startup)
    assert r.resolve("Copilot", PRODUCT).method == NEW_ENTITY


# ============================ determinism ============================
def test_pure_repeatable(resolver):
    a = resolver.resolve("HuggingFace", STARTUP)
    b = resolver.resolve("HuggingFace", STARTUP)
    assert a == b


def test_insertion_order_independent():
    rows_fwd = [TableRow("Zeta Labs"), TableRow("Alpha Labs"), TableRow("Mu Labs")]
    r1 = Resolver(seed_path=SEED, config_path=CFG, startup_rows=rows_fwd)
    r2 = Resolver(seed_path=SEED, config_path=CFG, startup_rows=list(reversed(rows_fwd)))
    for name in ["alpha labs", "Zeta Labs", "Mu Labz", "unknown co"]:
        assert r1.resolve(name, STARTUP) == r2.resolve(name, STARTUP)


# ============================ normalize helpers ============================
def test_compact_key_variants():
    for raw in ["OpenAI", "Open AI", "open-ai", "OpenAI, Inc.", "OPENAI", "The OpenAI"]:
        assert compact_key(raw) == "openai"
    assert compact_key("Foo and Bar") == compact_key("Foo & Bar")


def test_clean_display_name_keeps_casing():
    assert clean_display_name("  Acme   Robotics  Inc. ") == "Acme Robotics"
    assert clean_display_name("iRobot") == "iRobot"


def test_extract_domain():
    assert extract_domain("https://openai.com/about") == "openai.com"
    assert extract_domain("www.openai.com") == "openai.com"
    assert extract_domain("Scale AI") is None
