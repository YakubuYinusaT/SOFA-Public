"""Seed a demo merchant (CI Store) matching the spec's target conversation.

    python -m scripts.seed
"""

from sqlalchemy import select

from sofa.config import get_settings
from sofa.db import init_db, make_engine, make_session_factory
from sofa.gateway import links, mockbank
from sofa.models import Customer, Merchant, MerchantUser, MockBankIdentity, PhoneNumber, Product, ProductAlias, ProductUnit, Unit
from sofa.textutil import UNIT_VARIANTS

DEMO_NUMBER = "+2348001112222"
OWNER_PHONE = "+2348033333333"

# Demo subscribers for the gateway number (python -m scripts.simulate_call --dest <GATEWAY_NUMBER> --caller <phone>):
# phone, name, banks connected to that number. The last has none, which is the "connect me to a bank" conversation.
SUBSCRIBERS = [
    ("+2348055550101", "Amina", ["demobank"]),
    ("+2348055550102", "Tunde", ["demobank", "gtbank"]),
    ("+2348055550103", None, []),
]

# An existing Demo Bank customer who has not connected Sofa yet: for trying the connect-my-account call.
DEMO_LINK = {"phone": "+2348055550104", "account": "2012345678", "dob": "1996-03-14", "bvn": "22334455667"}

BALANCES = {("+2348055550101", "demobank"): 42_500, ("+2348055550102", "demobank"): 150_000, ("+2348055550102", "gtbank"): 8_200}

# name, brand, category, default_unit, price_naira, stock (default units), extra units, aliases
PRODUCTS = [
    ("Indomie Super Pack", "Indomie", "noodles", "pack", 350, 800, [("carton", 40, 7000)], ["indomie big", "super pack", "big indomie"]),
    ("Indomie Hungry Man", "Indomie", "noodles", "pack", 500, 400, [("carton", 20, 9500)], ["indomie big", "hungry man", "big indomie"]),
    ("Peak Milk Tin", "Peak", "milk", "tin", 800, 200, [], ["peak milk tin"]),
    ("Peak Milk Sachet", "Peak", "milk", "sachet", 100, 500, [], ["peak sachet"]),
    ("Golden Penny Spaghetti", "Golden Penny", "pasta", "pack", 900, 0, [], ["golden penny spaghetti", "spaghetti"]),
    ("Dangote Spaghetti", "Dangote", "pasta", "pack", 850, 50, [], ["dangote spaghetti"]),
    ("Dangote Sugar 1kg", "Dangote", "sugar", "pack", 1800, 100, [], ["sugar", "dangote sugar"]),
]


def seed(session_factory) -> Merchant:
    with session_factory() as db:
        existing = db.scalar(select(Merchant).where(Merchant.name == "CI Store"))
        if existing:
            return existing
        for canonical, variants in UNIT_VARIANTS.items():
            if not db.scalar(select(Unit).where(Unit.canonical == canonical)):
                db.add(Unit(canonical=canonical, variants=variants))
        m = Merchant(name="CI Store", category="provisions", default_language="en", payment_mode="transfer_first")
        db.add(m)
        db.flush()
        db.add(MerchantUser(merchant_id=m.id, name="Ade", phone=OWNER_PHONE, role="owner", can_update_stock=True))
        db.add(PhoneNumber(merchant_id=m.id, e164=DEMO_NUMBER))
        for name, brand, cat, unit, price, stock, units, aliases in PRODUCTS:
            p = Product(merchant_id=m.id, name=name, brand=brand, category=cat, default_unit=unit,
                        price_kobo=price * 100, stock_qty=stock)
            db.add(p)
            db.flush()
            db.add(ProductUnit(product_id=p.id, unit=unit, qty_per_unit=1, price_kobo=price * 100))
            for u, per, uprice in units:
                db.add(ProductUnit(product_id=p.id, unit=u, qty_per_unit=per, price_kobo=uprice * 100))
            for phrase in aliases:
                db.add(ProductAlias(merchant_id=m.id, product_id=p.id, phrase=phrase, source="seed"))
        db.commit()
        return m


def seed_subscribers(session_factory) -> None:
    with session_factory() as db:
        for phone, name, banks in SUBSCRIBERS:
            customer = db.scalar(select(Customer).where(Customer.phone == phone))
            if not customer:
                customer = Customer(phone=phone, name=name)
                db.add(customer)
                db.flush()
            for code in banks:
                links.link(db, customer.id, "banking", code)
                if not mockbank.MockBank(None).account(db, customer.id, code):  # the bank's side, for the demo
                    mockbank.open_account(db, customer, code, BALANCES.get((phone, code), 25_000))
                    if phone == "+2348055550101":  # the pitch's example: "send the usual to Mama"
                        # the same name at two banks, so SOFA has to ask which account
                        mockbank.add_beneficiary(db, customer, code, "Hauwa Bello", "mama, mum", 10_000, "GT Bank", "0123456789")
                        mockbank.add_beneficiary(db, customer, code, "Hauwa Bello", "mama", 5_000, "Access Bank", "0987654321")
        # the bank's records of people who already bank there but have not connected Sofa: for the "connect my account" demo.
        # Phone +2348055550104 is Kemi: account 2012345678, date of birth 14/03/1996, BVN 22334455667.
        if not db.scalar(select(MockBankIdentity).where(MockBankIdentity.account_number == DEMO_LINK["account"])):
            mockbank.add_identity(db, "demobank", DEMO_LINK["account"], "Kemi Ade", DEMO_LINK["dob"], DEMO_LINK["bvn"], balance_naira=61_000)
        db.commit()


if __name__ == "__main__":
    engine = make_engine(get_settings().database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    m = seed(factory)
    seed_subscribers(factory)
    print(f"Seeded merchant {m.name} ({m.id}); dial {DEMO_NUMBER}, owner {OWNER_PHONE}")
    print(f"To try connecting an existing account, call from {DEMO_LINK['phone']}: date of birth 14 03 1996, BVN {DEMO_LINK['bvn']}, account {DEMO_LINK['account']}.")
    print("Demo subscribers for the gateway number: " + ", ".join(f"{p} ({n or 'new'}: {', '.join(b) or 'no bank'})" for p, n, b in SUBSCRIBERS))
