"""Catalog changes shared by the CSV import, the admin pages and the admin API.
Stock never changes without a stock_movements row."""

import csv
import io
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Product, ProductAlias, ProductUnit, StockMovement
from ..textutil import canonical_unit, normalize_phrase

SAMPLE_CSV = (
    "name,brand,size_label,category,unit,price_naira,stock_qty,aliases,units\n"
    'Indomie Super Pack,Indomie,120g,noodles,pack,350,800,indomie big|super pack|big indomie,carton:40:7000\n'
    'Peak Milk Tin,Peak,170g,milk,tin,800,200,peak milk tin,\n'
)


def _kobo(price_naira) -> int:
    value = float(price_naira)
    if value < 0:
        raise ValueError("price cannot be negative")
    return int(round(value * 100))


def parse_units_spec(spec: str, default_unit: str) -> list[tuple[str, int, int]]:
    """'carton:40:7000|pack:1:350' -> [(unit, qty_per_default_unit, price_kobo)], skipping the default unit."""
    out = []
    for part in filter(None, (spec or "").split("|")):
        try:
            unit, per, price = part.split(":")
            per_n = int(per)
            if per_n < 1:
                raise ValueError
            unit = canonical_unit(unit) or unit.strip()
            if unit != default_unit:
                out.append((unit, per_n, _kobo(price)))
        except ValueError:
            raise ValueError(f"units entry '{part}' must look like carton:40:7000 (unit:how many default units:price in naira)")
    return out


def add_product(db: Session, merchant_id, *, name: str, unit: str, price_naira, stock_qty=0, brand=None,
                size_label=None, category=None, aliases: str = "", units_spec: str = "") -> Product:
    if not name.strip():
        raise ValueError("name is required")
    unit = canonical_unit(unit) or (unit or "piece").strip().lower()
    p = Product(merchant_id=merchant_id, name=name.strip(), brand=brand or None, size_label=size_label or None,
                category=category or None, default_unit=unit, price_kobo=_kobo(price_naira), stock_qty=0)
    db.add(p)
    db.flush()
    db.add(ProductUnit(product_id=p.id, unit=unit, qty_per_unit=1, price_kobo=p.price_kobo))
    for u, per, price in parse_units_spec(units_spec, unit):
        db.add(ProductUnit(product_id=p.id, unit=u, qty_per_unit=per, price_kobo=price))
    for phrase in filter(None, (a.strip() for a in (aliases or "").split("|"))):
        add_alias(db, p, phrase, "seed")
    db.flush()
    stock = int(float(stock_qty or 0))
    if stock:
        set_stock(db, p, stock, source="web", reason="restock")
    return p


def import_products_csv(db: Session, merchant_id, text: str) -> int:
    """All-or-nothing: raises ValueError naming the row on the first bad line."""
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    missing = {"name", "unit", "price_naira"} - set(rows[0].keys() if rows else [])
    if not rows:
        raise ValueError("the file has no rows")
    if missing:
        raise ValueError(f"missing column(s): {', '.join(sorted(missing))}")
    created = 0
    with db.begin_nested():
        for n, r in enumerate(rows, 2):
            try:
                add_product(db, merchant_id, name=r["name"], unit=r["unit"], price_naira=r["price_naira"],
                            stock_qty=r.get("stock_qty") or 0, brand=r.get("brand"), size_label=r.get("size_label"),
                            category=r.get("category"), aliases=r.get("aliases") or "", units_spec=r.get("units") or "")
            except (ValueError, TypeError) as exc:
                raise ValueError(f"row {n}: {exc}")
            created += 1
    return created


def set_stock(db: Session, product: Product, new_qty: int, source: str = "web", reason: str = "adjustment") -> int:
    """Set stock to new_qty, recording the difference as a movement. Returns the delta."""
    if new_qty < 0:
        raise ValueError("stock cannot be negative")
    delta = int(new_qty) - product.stock_qty
    if delta:
        product.stock_qty = int(new_qty)
        db.add(StockMovement(merchant_id=product.merchant_id, product_id=product.id, delta=delta, reason=reason, source=source))
    return delta


def set_price(product: Product, price_naira, unit: str | None = None) -> None:
    kobo = _kobo(price_naira)
    unit = unit or product.default_unit
    if unit == product.default_unit:
        product.price_kobo = kobo
    for u in product.units:
        if u.unit == unit:
            u.price_kobo = kobo


def add_alias(db: Session, product: Product, phrase: str, source: str, language: str | None = None) -> ProductAlias | None:
    norm = normalize_phrase(phrase)
    if not norm:
        return None
    existing = next((a for a in product.aliases if a.phrase == norm), None)
    if existing:
        return existing
    alias = ProductAlias(merchant_id=product.merchant_id, product_id=product.id, phrase=norm, source=source,
                         confirmations=0 if source == "seed" else 1, language=language)
    db.add(alias)
    product.aliases.append(alias)
    return alias


def get_product(db: Session, merchant_id, product_id) -> Product | None:
    if isinstance(product_id, str):
        product_id = uuid.UUID(product_id)
    return db.scalar(select(Product).where(Product.id == product_id, Product.merchant_id == merchant_id))
