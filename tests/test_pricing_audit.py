"""Trial-vs-free-tier pricing audit (no LLM). A free trial is not a free tier."""
from src.pipeline.products import audit_trial_freemium, trial_freemium_verdict


def test_vetrec_trial_with_price_becomes_paid():
    # The real VetRec row: a 14-day free trial + a real monthly price.
    ev = "Start your 14-day free trial. Plans from $99/month per vet."
    assert trial_freemium_verdict("FREEMIUM", ev) == "PAID"


def test_trial_only_no_price_becomes_null():
    assert trial_freemium_verdict("FREEMIUM", "Start your free trial today.") is None
    assert trial_freemium_verdict("FREEMIUM", "14-day trial, no credit card") is None


def test_genuine_free_tier_is_left_freemium():
    # Has real free-tier wording -> genuinely freemium, untouched.
    assert trial_freemium_verdict(
        "FREEMIUM", "Free plan available. Pro is $20/month.") == "FREEMIUM"
    assert trial_freemium_verdict(
        "FREEMIUM", "Free forever for individuals; Team at $10/user/month.") == "FREEMIUM"
    assert trial_freemium_verdict(
        "FREEMIUM", "Open source and free; paid cloud at $49/month.") == "FREEMIUM"


def test_non_trial_freemium_untouched():
    # No trial wording at all -> rule does not apply.
    assert trial_freemium_verdict("FREEMIUM", "Starter and Pro plans.") == "FREEMIUM"


def test_other_labels_untouched():
    for pm in ("PAID", "FREE", "ENTERPRISE", None):
        assert trial_freemium_verdict(pm, "free trial only") == pm


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.updated = []

    def iter_products(self):
        return iter(self._rows)

    def update_product_pricing(self, slug, norm, pricing_model):
        self.updated.append((slug, norm, pricing_model))


def _row(slug, name, pm, ev):
    return {"startup_slug": slug, "normalized_name": name.lower(),
            "startup_name": slug, "product_name": name,
            "pricing_model": pm, "evidence_pricing": ev}


def test_audit_collects_and_applies(monkeypatch):
    import src.pipeline.products as P
    monkeypatch.setattr(P, "_export", lambda db: 0)   # skip file I/O

    rows = [
        _row("vetrec", "VetRec", "FREEMIUM",
             "Start your 14-day free trial. Plans from $99/month per vet."),  # -> PAID
        _row("acme", "Acme", "FREEMIUM", "Free trial for 7 days."),           # -> null
        _row("nimbus", "Nimbus", "FREEMIUM", "Free plan; Pro $20/month."),    # unchanged
        _row("paidco", "PaidCo", "PAID", "trial then $5/month"),              # unchanged
    ]
    db = _FakeDB(rows)
    changes = audit_trial_freemium(db, apply=True)

    by_product = {c["product"]: c for c in changes}
    assert set(by_product) == {"VetRec", "Acme"}
    assert by_product["VetRec"]["new"] == "PAID"
    assert by_product["Acme"]["new"] is None
    assert set(db.updated) == {("vetrec", "vetrec", "PAID"), ("acme", "acme", None)}
