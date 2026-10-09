"""Product matching: normalise, exact alias, fuzzy (trigram), N-ATLaS rerank, decide.

Aliases rank by source: merchant_correction > confirmed_call (by confirmations) > seed.
Trigram scoring is done in Python here (same measure as pg_trgm.similarity) since a pilot
catalog is small; swap for a SQL `similarity()` query when catalogs grow.
"""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product, ProductAlias
from ..textutil import clean, containment, normalize_phrase, similarity, tokens

SOURCE_RANK = {"merchant_correction": 3, "confirmed_call": 2, "global": 2, "seed": 1}


@dataclass
class MatchResult:
    status: str  # match | ambiguous | none
    product: Product | None = None
    candidates: list[tuple[Product, float]] = field(default_factory=list)
    step: str = ""  # which pipeline step decided (for the "problem solved" write-up)

    def log(self) -> dict:
        return {
            "status": self.status,
            "step": self.step,
            "chosen": self.product.name if self.product else None,
            "candidates": [{"product": p.name, "score": round(s, 3)} for p, s in self.candidates],
        }


def active_products(db: Session, merchant_id) -> list[Product]:
    return list(db.scalars(select(Product).where(Product.merchant_id == merchant_id, Product.active.is_(True))))


def _phrases(p: Product) -> list[str]:
    return [p.name, *(a.phrase for a in p.aliases)]


def _exact_alias(products: list[Product], phrase: str, spoken_clean: str = "") -> Product | None:
    """The spoken phrase is compared as spoken ("can fertilizer") and with filler words removed ("fertilizer"), each against the catalogue's own words
    kept whole. Removing "can" from a product called "CAN Fertilizer" made it the same as the plain word "fertilizer", so every "fertilizer" matched it."""
    wanted = {phrase, spoken_clean} - {""}
    hits: list[tuple[int, int, Product]] = []
    for p in products:
        for a in p.aliases:
            if clean(a.phrase) in wanted:
                hits.append((SOURCE_RANK.get(a.source, 0), a.confirmations, p))
        if clean(p.name) in wanted:
            hits.append((0, 0, p))
    if not hits:
        return None
    hits.sort(key=lambda h: (h[0], h[1]), reverse=True)
    top_products = {h[2].id for h in hits if (h[0], h[1]) == (hits[0][0], hits[0][1])}
    return hits[0][2] if len(top_products) == 1 else None


def fuzzy(products: list[Product], phrase: str, floor: float, limit: int = 5) -> list[tuple[Product, float]]:
    scored = [(p, max(max(similarity(phrase, ph), 0.9 * containment(phrase, ph)) for ph in _phrases(p))) for p in products]
    scored = [s for s in scored if s[1] > floor]
    scored.sort(key=lambda s: s[1], reverse=True)
    return scored[:limit]


async def match_product(
    db: Session, merchant_id, spoken: str, llm, settings, unit: str | None = None
) -> MatchResult:
    products = active_products(db, merchant_id)
    phrase = normalize_phrase(spoken)
    if not phrase and not clean(spoken):
        return MatchResult("none", step="empty")

    exact = _exact_alias(products, phrase, clean(spoken))
    if exact:
        return MatchResult("match", exact, [(exact, 1.0)], "exact_alias")

    pool = products
    if unit:  # a spoken unit narrows the field: "peak milk tin" excludes the sachet
        with_unit = [p for p in products if unit == p.default_unit or any(u.unit == unit for u in p.units)]
        pool = with_unit or products
    cands = fuzzy(pool, phrase, settings.match_score_floor)
    if not cands:
        return MatchResult("none", step="fuzzy_none")
    if len(cands) == 1:
        return MatchResult("match", cands[0][0], cands, "fuzzy_single")

    choice = await llm.rerank(phrase, [c[0].name for c in cands])
    if choice is not None:
        chosen = cands[choice - 1]
        others = [s for p, s in cands if p.id != chosen[0].id]
        if chosen[1] - max(others) >= settings.match_winner_gap:
            return MatchResult("match", chosen[0], cands, "rerank")
    return MatchResult("ambiguous", candidates=cands[:3], step="close_candidates")


def pick_among(candidates: list[Product], text: str, settings=None) -> Product | None:
    """Resolve a clarification answer ("super pack", "the tin") against the offered candidates."""
    words = tokens(text)
    if not words:
        return None
    scored = []
    for p in candidates:
        vocab = tokens(" ".join([p.name, p.size_label or "", p.default_unit, *(a.phrase for a in p.aliases)]))
        scored.append((len(words & vocab), max(similarity(clean(text), ph) for ph in _phrases(p)), p))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)
    if scored[0][0] == 0 and scored[0][1] < 0.3:
        return None
    if len(scored) > 1 and (scored[0][0], round(scored[0][1], 2)) == (scored[1][0], round(scored[1][1], 2)):
        return None
    return scored[0][2]


def substitute_for(products: list[Product], target: Product) -> Product | None:
    """Closest in-stock alternative in the same category."""
    pool = [p for p in products if p.id != target.id and p.stock_qty > 0 and p.category == target.category]
    if not pool:
        return None
    return max(pool, key=lambda p: similarity(target.name, p.name))


def learn_alias(db: Session, merchant_id, product: Product, spoken: str, language: str | None, source: str) -> None:
    """Called when a caller confirms a read-back (or a merchant corrects an order)."""
    phrase = normalize_phrase(spoken)
    if not phrase or phrase == normalize_phrase(product.name):
        return
    existing = next((a for a in product.aliases if normalize_phrase(a.phrase) == phrase), None)
    if existing:
        existing.confirmations += 1
        if SOURCE_RANK.get(source, 0) > SOURCE_RANK.get(existing.source, 0):
            existing.source = source
    else:
        alias = ProductAlias(
            merchant_id=merchant_id, product_id=product.id, phrase=phrase,
            language=language, source=source, confirmations=1,
        )
        db.add(alias)
        product.aliases.append(alias)
