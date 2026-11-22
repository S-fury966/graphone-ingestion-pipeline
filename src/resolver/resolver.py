"""Deterministic entity resolver for STARTUP and PRODUCT names (Step 13).

No LLM anywhere: resolution is purely deterministic and order-independent.
Precision is valued over recall — a wrong merge is worse than a missed merge, so
every layer is conservative and the fuzzy layer only fires when every guard in
config/resolver.yaml passes.

Layers (first success wins):
  1. EXACT      — compact-key match against seed canonicals + aliases.
  2. DOMAIN     — a domain in the raw string matched to a seed domain.
  3. TABLE      — our own startups/products table (normalized name or domain).
  4. FUZZY      — rapidfuzz on compact keys, guarded hard.
  5. NEW_ENTITY — unresolved; canonical = cleaned raw string.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import yaml
from rapidfuzz import fuzz

from .normalize import (clean_display_name, compact_key, normalize_name,
                        registrable_domain)

EXACT = "EXACT"
DOMAIN = "DOMAIN"
TABLE = "TABLE"
FUZZY = "FUZZY"
NEW_ENTITY = "NEW_ENTITY"

STARTUP = "STARTUP"
PRODUCT = "PRODUCT"

_CONFIG_DEFAULT = "config/resolver.yaml"
_SEED_DEFAULT = "config/seed_entities.yaml"

# A domain-like token: at least one dot, letters/digits/hyphens only.
_DOMAIN_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9\-]*(?:\.[A-Za-z0-9\-]+)+)")


@dataclass(frozen=True)
class Resolution:
    canonical: str
    method: str
    score: Optional[float]          # 0..1 for FUZZY; 1.0 for EXACT/DOMAIN/TABLE; None for NEW
    matched_alias: Optional[str]    # the seed/table string we matched on, if any


@dataclass(frozen=True)
class TableRow:
    """A row from our own startups/products table, pre-normalized by the caller."""
    canonical: str
    domain: Optional[str] = None
    startup: Optional[str] = None   # for product rows: the owning startup canonical


def extract_domain(raw: str) -> Optional[str]:
    """Return the registrable domain embedded in `raw`, or None.

    Handles bare domains ("openai.com"), hosts ("www.openai.com") and URLs
    ("https://openai.com/about"). A plain name with no dot ("Scale AI") yields
    None, so this never fires on ordinary company names.
    """
    if not raw:
        return None
    m = _DOMAIN_RE.search(raw.strip())
    if not m:
        return None
    return registrable_domain(m.group(1))


class Resolver:
    def __init__(
        self,
        seed_path: str | Path = _SEED_DEFAULT,
        config_path: str | Path = _CONFIG_DEFAULT,
        startup_rows: Optional[Iterable[TableRow]] = None,
        product_rows: Optional[Iterable[TableRow]] = None,
    ) -> None:
        cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        fz = cfg.get("fuzzy", {})
        self.min_score: float = float(fz.get("min_score", 0.93))
        self.min_len: int = int(fz.get("min_compact_len", 5))
        self.same_first: bool = bool(fz.get("require_same_first_char", True))
        self.min_margin: float = float(fz.get("min_margin", 0.02))
        self.legal_suffixes: set = set(cfg.get("legal_suffixes", [])) or None

        seed = yaml.safe_load(Path(seed_path).read_text(encoding="utf-8")) or {}
        entities = seed.get("seed_entities") or []

        # ---- seed indices (startups) ----
        self._exact: dict[str, tuple[str, str]] = {}        # compact -> (canonical, alias)
        self._domain: dict[str, str] = {}                   # domain  -> canonical
        self._fuzzy: list[tuple[str, str, str]] = []        # (compact, canonical, alias)
        # ---- seed indices (products) ----
        self._prod_exact: dict[tuple[str, str], tuple[str, str]] = {}
        self._prod_fuzzy: dict[str, list[tuple[str, str, str]]] = {}
        self._prod_global: dict[str, set[tuple[str, str]]] = {}  # compact -> {(startup, prod)}

        for ent in entities:
            canon = ent["canonical"]
            self._add_startup_key(canon, canon)
            for alias in ent.get("aliases") or []:
                self._add_startup_key(alias, canon, alias=alias)
            dom = ent.get("domain")
            if dom:
                d = registrable_domain(dom)
                if d:
                    self._domain.setdefault(d, canon)
            for prod in ent.get("products") or []:
                pcanon = prod["canonical"]
                self._add_product_key(canon, pcanon, pcanon)
                for palias in prod.get("aliases") or []:
                    self._add_product_key(canon, pcanon, palias, alias=palias)

        # ---- do_not_merge: frozensets of compact keys ----
        self._dnm: set[frozenset[str]] = set()
        for pair in seed.get("do_not_merge") or []:
            if len(pair) == 2:
                a, b = self._key(pair[0]), self._key(pair[1])
                if a and b:
                    self._dnm.add(frozenset({a, b}))

        # ---- our own tables (optional) ----
        self._tbl_name: dict[str, str] = {}
        self._tbl_domain: dict[str, str] = {}
        self._tbl_fuzzy: list[tuple[str, str, str]] = []
        for row in startup_rows or []:
            self._add_table_row(row)
        # product table rows become scoped product candidates
        self._prod_tbl_exact: dict[tuple[str, str], str] = {}
        self._prod_tbl_fuzzy: dict[str, list[tuple[str, str, str]]] = {}
        self._prod_tbl_global: dict[str, set[tuple[str, str]]] = {}
        for row in product_rows or []:
            self._add_product_table_row(row)

    # ---------- resolver key ----------
    def _key(self, raw: str) -> str:
        """Resolver-only compact key. Builds on normalize_name (unchanged, so the
        startups/products dedup keys it feeds are untouched) and additionally:
          * drops a single leading article "the",
          * drops standalone "and" joiners,
          * peels a TRAILING legal suffix from config (self.legal_suffixes) only
            while it is the last token AND the remaining compact key is >= 4 chars,
          * collapses a trailing run of single letters that joins into a known
            legal suffix ("l l c" -> "llc", "s a s" -> "sas", "p b c" -> "pbc",
            "i n c" -> "inc") and strips it, again only if >= 4 chars remain. A
            single trailing letter that is part of a name ("Vitamin C", "Team A")
            is never stripped.
        """
        norm = normalize_name(raw)
        if not norm:
            return ""
        tokens = [t for t in norm.split(" ") if t]
        if len(tokens) > 1 and tokens[0] == "the":
            tokens = tokens[1:]
        tokens = [t for t in tokens if t != "and"] or tokens
        suffixes = self.legal_suffixes or set()

        # Dotted abbreviation: a trailing run of >= 2 single-letter tokens that
        # joins into a known suffix (from "L.L.C." / "S.A.S." / "P.B.C.").
        i = len(tokens)
        while i > 0 and len(tokens[i - 1]) == 1:
            i -= 1
        trailing_singles = tokens[i:]
        if len(trailing_singles) >= 2:
            joined = "".join(trailing_singles)
            if joined in suffixes and len("".join(tokens[:i])) >= 4:
                tokens = tokens[:i]

        # Whole-word trailing legal suffix ("... SAS", "... Company").
        while (len(tokens) > 1 and tokens[-1] in suffixes
               and len("".join(tokens[:-1])) >= 4):
            tokens.pop()
        return "".join(tokens)

    # ---------- index builders ----------
    def _add_startup_key(self, raw: str, canonical: str, alias: str | None = None) -> None:
        ck = self._key(raw)
        if not ck:
            return
        self._exact.setdefault(ck, (canonical, alias or raw))
        self._fuzzy.append((ck, canonical, alias or raw))

    def _add_product_key(self, startup: str, canonical: str, raw: str,
                         alias: str | None = None) -> None:
        ck = self._key(raw)
        if not ck:
            return
        self._prod_exact.setdefault((startup, ck), (canonical, alias or raw))
        self._prod_fuzzy.setdefault(startup, []).append((ck, canonical, alias or raw))
        self._prod_global.setdefault(ck, set()).add((startup, canonical))

    def _add_table_row(self, row: TableRow) -> None:
        nn = normalize_name(row.canonical)
        if nn:
            self._tbl_name.setdefault(nn, row.canonical)
            self._tbl_fuzzy.append((self._key(row.canonical), row.canonical, row.canonical))
        if row.domain:
            d = registrable_domain(row.domain)
            if d:
                self._tbl_domain.setdefault(d, row.canonical)

    def _add_product_table_row(self, row: TableRow) -> None:
        if not row.startup:
            return
        ck = self._key(row.canonical)
        if not ck:
            return
        key = (row.startup, ck)
        self._prod_tbl_exact.setdefault(key, row.canonical)
        self._prod_tbl_fuzzy.setdefault(row.startup, []).append((ck, row.canonical, row.canonical))
        self._prod_tbl_global.setdefault(ck, set()).add((row.startup, row.canonical))

    # ---------- public API ----------
    def resolve(self, raw_name: str, entity_type: str = STARTUP,
                startup: Optional[str] = None) -> Resolution:
        if entity_type == PRODUCT:
            return self._resolve_product(raw_name, startup)
        return self._resolve_startup(raw_name)

    # ---------- startup resolution ----------
    def _resolve_startup(self, raw_name: str) -> Resolution:
        raw = (raw_name or "").strip()
        if not raw:
            return Resolution("", NEW_ENTITY, None, None)

        ck = self._key(raw)

        # 1. exact on compact key
        if ck and ck in self._exact:
            canon, alias = self._exact[ck]
            return Resolution(canon, EXACT, 1.0, alias)

        # 2. domain match against the seed
        dom = extract_domain(raw)
        if dom and dom in self._domain:
            return Resolution(self._domain[dom], DOMAIN, 1.0, dom)

        # 3. our own startups table (name, then domain)
        nn = normalize_name(raw)
        if nn and nn in self._tbl_name:
            return Resolution(self._tbl_name[nn], TABLE, 1.0, None)
        if dom and dom in self._tbl_domain:
            return Resolution(self._tbl_domain[dom], TABLE, 1.0, dom)

        # 4. conservative fuzzy on compact keys (seed + table candidates)
        fz = self._fuzzy_match(ck, self._fuzzy + self._tbl_fuzzy)
        if fz is not None:
            return fz

        # 5. unresolved
        return Resolution(clean_display_name(raw, self.legal_suffixes), NEW_ENTITY, None, None)

    # ---------- product resolution ----------
    def _resolve_product(self, raw_name: str, startup: Optional[str]) -> Resolution:
        raw = (raw_name or "").strip()
        if not raw:
            return Resolution("", NEW_ENTITY, None, None)
        ck = self._key(raw)

        startup_canon: Optional[str] = None
        if startup:
            s = self._resolve_startup(startup)
            if s.method != NEW_ENTITY:
                startup_canon = s.canonical

        if startup_canon:
            # scoped: only this startup's products can match
            key = (startup_canon, ck)
            if key in self._prod_exact:
                canon, alias = self._prod_exact[key]
                return Resolution(canon, EXACT, 1.0, alias)
            if key in self._prod_tbl_exact:
                return Resolution(self._prod_tbl_exact[key], TABLE, 1.0, None)
            cands = (self._prod_fuzzy.get(startup_canon, [])
                     + self._prod_tbl_fuzzy.get(startup_canon, []))
            fz = self._fuzzy_match(ck, cands)
            if fz is not None:
                return fz
            return Resolution(clean_display_name(raw, self.legal_suffixes), NEW_ENTITY, None, None)

        # unscoped: only accept an UNAMBIGUOUS exact product (same product name
        # under two startups stays separate -> NEW_ENTITY).
        owners = (self._prod_global.get(ck, set()) | self._prod_tbl_global.get(ck, set()))
        if len(owners) == 1:
            _, pcanon = next(iter(owners))
            return Resolution(pcanon, EXACT, 1.0, None)
        return Resolution(clean_display_name(raw, self.legal_suffixes), NEW_ENTITY, None, None)

    # ---------- diagnostics ----------
    def audit_seed_collisions(self, threshold: float = 0.88
                              ) -> list[tuple[str, str, float, str]]:
        """Report pairs of DIFFERENT seed canonicals whose compact keys are close
        (ratio >= threshold), and why the resolver still keeps them apart. Used to
        prove no two seed companies are at risk of a wrong merge."""
        canon_keys: dict[str, str] = {}
        for ck, canonical, alias in self._fuzzy:
            if alias == canonical:            # the canonical's own entry
                canon_keys[canonical] = ck
        items = sorted(canon_keys.items())
        out: list[tuple[str, str, float, str]] = []
        for i, (ca, ka) in enumerate(items):
            for cb, kb in items[i + 1:]:
                if not ka or not kb:
                    continue
                if len(ka) < self.min_len or len(kb) < self.min_len:
                    continue
                if self.same_first and ka[0] != kb[0]:
                    continue
                s = fuzz.ratio(ka, kb) / 100.0
                if s < threshold:
                    continue
                reasons = []
                if s < self.min_score:
                    reasons.append(f"below min_score {self.min_score}")
                if frozenset({ka, kb}) in self._dnm:
                    reasons.append("do_not_merge")
                blocked = "; ".join(reasons) or "margin/exact-identity wins"
                out.append((ca, cb, round(s, 4), blocked))
        return sorted(out, key=lambda x: -x[2])

    # ---------- fuzzy core ----------
    def _fuzzy_match(self, ck: str, candidates: list[tuple[str, str, str]]
                     ) -> Optional[Resolution]:
        if not ck or len(ck) < self.min_len:
            return None
        scored: list[tuple[float, str, str, str]] = []
        for cand_ck, canonical, alias in candidates:
            if not cand_ck or len(cand_ck) < self.min_len:
                continue
            if self.same_first and cand_ck[0] != ck[0]:
                continue
            s = fuzz.ratio(ck, cand_ck) / 100.0
            scored.append((s, canonical, cand_ck, alias))
        if not scored:
            return None
        # deterministic order: score desc, then canonical, then compact key
        scored.sort(key=lambda x: (-x[0], x[1], x[2]))
        best_s, best_canon, best_ck, best_alias = scored[0]
        if best_s < self.min_score:
            return None
        # margin vs the best candidate with a DIFFERENT canonical
        for s, canonical, _ck, _al in scored[1:]:
            if canonical != best_canon:
                if (best_s - s) < self.min_margin:
                    return None
                break
        # do_not_merge guard
        if frozenset({ck, best_ck}) in self._dnm:
            return None
        return Resolution(best_canon, FUZZY, round(best_s, 4), best_alias)
