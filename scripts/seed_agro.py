"""CII Store as an agriculture end-to-end store: farm inputs to buy, and advice on using them.

    python -m scripts.seed_agro

The store's starting prices, stock and general farming guidance (the vendor portal at /vendor edits all of it). Sofa speaks the advice exactly as it is written here: it never makes farming advice up.
"""

from sqlalchemy import select

from sofa.config import get_settings
from sofa.db import init_db, make_engine, make_session_factory
from sofa.models import Merchant, MerchantUser, PhoneNumber, Product, ProductAlias, ProductUnit, ShopAdvice, Unit
from sofa.textutil import UNIT_VARIANTS

from scripts.seed import DEMO_NUMBER, OWNER_PHONE, seed_subscribers

# name, brand, category, default_unit, price_naira, stock, extra units [(unit, per, price)], spoken aliases
PRODUCTS = [
    ("NPK 15-15-15 Fertilizer", None, "fertilizer", "bag", 38_000, 300, [], ["npk", "npk fertilizer", "fifteen fifteen fifteen", "15 15 15", "compound fertilizer"]),
    ("Urea Fertilizer", None, "fertilizer", "bag", 34_000, 250, [], ["urea", "urea fertilizer", "46 percent urea"]),
    ("CAN Fertilizer", None, "fertilizer", "bag", 30_000, 120, [], ["can", "can fertilizer", "calcium ammonium nitrate"]),
    ("SSP Fertilizer", None, "fertilizer", "bag", 22_000, 0, [], ["ssp", "single super phosphate", "phosphate fertilizer"]),
    ("Organic Manure", None, "fertilizer", "bag", 6_500, 400, [], ["manure", "organic manure", "organic fertilizer"]),
    ("Hybrid Maize Seed", None, "seed", "pack", 9_500, 180, [], ["maize seed", "corn seed", "hybrid maize"]),
    ("Improved Rice Seed", None, "seed", "bag", 12_000, 90, [], ["rice seed", "improved rice"]),
    ("Tomato Seed", None, "seed", "sachet", 3_500, 200, [], ["tomato seed", "tomato seeds"]),
    ("Glyphosate Herbicide", None, "agrochemical", "bottle", 4_500, 220, [("litre", 1, 4_500)], ["glyphosate", "round up", "weedicide", "weed killer", "herbicide"]),
    ("Atrazine Herbicide", None, "agrochemical", "bottle", 5_200, 140, [("litre", 1, 5_200)], ["atrazine", "maize weedicide", "maize herbicide"]),
    ("Cypermethrin Insecticide", None, "agrochemical", "bottle", 6_800, 160, [("litre", 1, 6_800)], ["cypermethrin", "insecticide", "pesticide", "army worm spray"]),
    ("Mancozeb Fungicide", None, "agrochemical", "pack", 3_900, 110, [], ["mancozeb", "fungicide"]),
    ("Knapsack Sprayer 16 Litre", None, "tools", "piece", 28_000, 35, [], ["sprayer", "knapsack", "knapsack sprayer"]),
    ("Hand Hoe", None, "tools", "piece", 2_800, 150, [], ["hoe", "hand hoe"]),
    ("Protective Gloves", None, "tools", "piece", 1_500, 300, [], ["gloves", "protective gloves"]),
]

# topic, words a caller would use, what Sofa says (the owner's words), the product Sofa then offers
ADVICE = [
    ("Fertilizer for maize",
     "maize, corn, fertilizer, fertiliser, npk, urea, apply, bags, acre, acres",
     "A common guide for maize is about two bags of NPK 15-15-15 for each acre at planting or within two weeks after, then about one bag of urea "
     "for each acre four to six weeks later. Put it a hand's width from the plant, never on the leaves, and only when the soil is moist. "
     "Soil and variety change the amount.",
     "NPK 15-15-15 Fertilizer"),
    ("Fertilizer for rice",
     "rice, paddy, fertilizer, fertiliser, npk, urea, apply, bags, acre, acres",
     "For rice, put NPK in about two weeks after sowing or transplanting, and urea in two parts, the first when the plants start to tiller and "
     "the second when the flower stalks begin to form. Keep a little water on the field when you apply, and do not let the fertilizer wash away.",
     "Urea Fertilizer"),
    ("Fertilizer for tomatoes",
     "tomato, tomatoes, fertilizer, fertiliser, npk, apply, flowering, fruit, fruiting",
     "For tomatoes, put a small amount of NPK around each plant about two weeks after transplanting, a hand's width from the stem, then repeat "
     "every three weeks while the plants are flowering and fruiting. Water after you apply, and keep the fertilizer off the leaves.",
     "NPK 15-15-15 Fertilizer"),
    ("Weed control with glyphosate",
     "weed, weeds, weedicide, herbicide, glyphosate, spray, round up, grass",
     "Glyphosate kills any green plant it touches, so spray only on weeds before planting or between rows with a shield on the nozzle. Spray in calm "
     "weather and not when rain is expected within six hours. Wear gloves, a mask and boots, and wash the sprayer well afterwards. Follow the label dose.",
     "Glyphosate Herbicide"),
    ("Fall armyworm in maize",
     "armyworm, army worm, worm, worms, pest, pests, insect, insecticide, maize, corn, leaves, spray",
     "Look for ragged leaves and sawdust-like droppings inside the leaf whorl. Spray an insecticide such as cypermethrin early in the morning or late "
     "in the afternoon, aiming into the whorl, and follow the label dose. Check again after seven to ten days, and spray again if the worms return.",
     "Cypermethrin Insecticide"),
    ("Storing fertilizer safely",
     "store, storing, storage, keep, fertilizer, fertiliser, bags, safe, safely",
     "Keep fertilizer bags closed, dry and off the floor, away from fire, fuel and children. Use a bag that has been opened first, and never mix "
     "different fertilizers or chemicals in one bag.",
     None),
    ("Using organic manure",
     "manure, organic, compost, soil, improve, spread, mix",
     "Spread organic manure on the field and mix it into the soil about two weeks before planting, so it has time to settle. It improves the soil "
     "over seasons, and works well together with NPK.",
     "Organic Manure"),
]


def seed_agro(session_factory) -> Merchant:
    """Create CII Store as an agriculture store (or return it if it exists)."""
    with session_factory() as db:
        existing = db.scalar(select(Merchant).where(Merchant.name == "CI Store"))
        if existing:
            return existing
        for canonical, variants in UNIT_VARIANTS.items():
            if not db.scalar(select(Unit).where(Unit.canonical == canonical)):
                db.add(Unit(canonical=canonical, variants=variants))
        m = Merchant(name="CI Store", category="agriculture", default_language="en", payment_mode="transfer_first")
        db.add(m)
        db.flush()
        db.add(MerchantUser(merchant_id=m.id, name="Ade", phone=OWNER_PHONE, role="owner", can_update_stock=True))
        db.add(PhoneNumber(merchant_id=m.id, e164=DEMO_NUMBER))
        for name, brand, cat, unit, price, stock, units, aliases in PRODUCTS:
            p = Product(merchant_id=m.id, name=name, brand=brand, category=cat, default_unit=unit, price_kobo=price * 100, stock_qty=stock)
            db.add(p)
            db.flush()
            db.add(ProductUnit(product_id=p.id, unit=unit, qty_per_unit=1, price_kobo=price * 100))
            for u, per, uprice in units:
                if u != unit:
                    db.add(ProductUnit(product_id=p.id, unit=u, qty_per_unit=per, price_kobo=uprice * 100))
            for phrase in aliases:
                db.add(ProductAlias(merchant_id=m.id, product_id=p.id, phrase=phrase, source="seed"))
        for topic, keywords, answer, product in ADVICE:
            db.add(ShopAdvice(merchant_id=m.id, topic=topic, keywords=keywords, answer=answer, product_name=product))
        db.commit()
        return m


if __name__ == "__main__":
    engine = make_engine(get_settings().database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    m = seed_agro(factory)
    seed_subscribers(factory)
    print(f"Seeded {m.name} ({m.category}); dial {DEMO_NUMBER}, owner {OWNER_PHONE}. Edit prices, stock and advice in /vendor.")
