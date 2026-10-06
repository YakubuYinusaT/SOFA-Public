import asyncio
import re
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from sofa.models import Call, DailyReport, Handoff, Merchant, MissedDemand, Notification, Order
from sofa.services import reports, scheduler
from tests.conftest import OWNER
from tests.test_call_flow import MUSA, SPEC_CONVERSATION
from tests.test_admin_pages import login, post, flash

UTC = timezone.utc
# 19:00 in Lagos (UTC+1) is 18:00 UTC. All scheduler tests use this fixed day, never the real clock.
DAY = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
AT_7PM = datetime(2026, 9, 29, 18, 0, tzinfo=UTC)


@pytest.fixture
def busy_day(call, db):
    """One confirmed order, one missed request and one open handoff, all dated on the fixed day."""
    call(SPEC_CONVERSATION)
    call(["Do you have pizza", "Let me talk to the owner"], caller="+2348055550002")
    o = db.scalar(select(Order))
    o.confirmed_at = DAY
    for m in db.scalars(select(MissedDemand)):
        m.created_at = DAY
    db.commit()
    return db.scalar(select(Merchant))


def merchant_of(db):
    return db.scalar(select(Merchant))


# ---- the numbers ------------------------------------------------------------------------------------


def test_report_counts_orders_value_unpaid_missed_and_callbacks(busy_day, db):
    r = reports.compute_report(db, busy_day, AT_7PM)
    assert (r.orders, r.value_kobo, r.unpaid, r.callbacks) == (1, 1_640_000, 1, 1)
    assert r.missed == [("pizza", 1)] and r.day == "2026-09-29" and not r.is_empty


def test_missed_requests_are_grouped_across_spellings_and_counted(busy_day, db):
    for spoken in ("Pizza", "pizza please", "a pizza"):
        db.add(MissedDemand(merchant_id=busy_day.id, spoken_name=spoken, created_at=DAY))
    db.commit()
    assert reports.compute_report(db, busy_day, AT_7PM).missed == [("pizza", 4)]


def test_day_is_the_lagos_day_not_the_utc_day(busy_day, db):
    o = db.scalar(select(Order))
    for confirmed, counted in [
        (datetime(2026, 9, 28, 22, 59, tzinfo=UTC), False),  # 23:59 Lagos the day before
        (datetime(2026, 9, 28, 23, 0, tzinfo=UTC), True),    # 00:00 Lagos: the day starts
        (datetime(2026, 9, 29, 22, 59, tzinfo=UTC), True),   # 23:59 Lagos
        (datetime(2026, 9, 29, 23, 0, tzinfo=UTC), False),   # 00:00 Lagos the next day
    ]:
        o.confirmed_at = confirmed
        db.commit()
        assert reports.compute_report(db, busy_day, AT_7PM).orders == (1 if counted else 0), confirmed


def test_cancelled_orders_and_test_calls_are_left_out(busy_day, db):
    o = db.scalar(select(Order))
    o.status = "cancelled"
    db.commit()
    assert reports.compute_report(db, busy_day, AT_7PM).orders == 0
    o.status = "awaiting_payment"
    call = db.get(Call, o.source_call_id)
    call.is_test = True
    db.commit()
    assert reports.compute_report(db, busy_day, AT_7PM).orders == 0
    assert reports.compute_report(db, busy_day, AT_7PM, include_test=True).orders == 1  # only sample sends count them


# ---- the SMS -------------------------------------------------------------------------------------------


def test_sms_is_one_plain_ascii_segment():
    r = reports.Report("CI Store", "2026-09-29", orders=12, value_kobo=18_450_000, unpaid=3,
                       missed=[("golden penny spaghetti", 5), ("pizza", 2)], callbacks=2)
    text = reports.sms_text(r)
    assert text == "CI Store 29Sep: 12 orders, N184,500, 3 unpaid. Missed: Golden Penny Spaghetti x5, Pizza x2. 2 callbacks due."
    assert len(text) <= 160 and text.isascii() and "₦" not in text


def test_sms_uses_singulars_and_drops_empty_parts():
    one = reports.sms_text(reports.Report("CI Store", "2026-09-05", orders=1, value_kobo=350_000, callbacks=1))
    assert one == "CI Store 5Sep: 1 order, N3,500. 1 callback due."
    quiet = reports.sms_text(reports.Report("CI Store", "2026-09-05", missed=[("pizza", 1)]))
    assert quiet == "CI Store 5Sep: no orders. Missed: Pizza x1."


def test_sms_never_exceeds_one_segment_and_keeps_what_matters_most():
    many = [(f"very long product name number {i} with extras", 9 - i % 9) for i in range(30)]
    r = reports.Report("A Really Long Business Name Ltd", "2026-09-29", orders=123, value_kobo=987_654_321_00, unpaid=45, missed=many, callbacks=6)
    text = reports.sms_text(r)
    assert len(text) <= 160
    assert "123 orders" in text and "45 unpaid" in text and "6 callbacks due" in text  # the priorities survive
    assert "Missed: Very Long" in text  # as many missed items as still fit, at least one here


def test_sms_strips_characters_that_would_double_the_cost():
    r = reports.Report("Bola's Pharmacy ₦\U0001F600", "2026-09-29", orders=2, value_kobo=100_000,
                       missed=[("café au lait ’s", 1)])
    text = reports.sms_text(r)
    assert text.isascii() and len(text) <= 160 and "Bola's Pharmacy" in text and "Cafe Au Lait" in text


# ---- the 7 PM scheduler --------------------------------------------------------------------------------------


def run(svc, now):
    return asyncio.run(scheduler.run_due_reports(svc, now))


def test_report_goes_out_once_at_7pm_and_never_twice(busy_day, app, db):
    svc = app.state.svc
    assert run(svc, datetime(2026, 9, 29, 17, 59, tzinfo=UTC)) == []  # 18:59 Lagos: not yet
    assert not db.scalars(select(DailyReport)).all()
    assert run(svc, AT_7PM) == ["CI Store"]
    assert run(svc, AT_7PM + timedelta(minutes=1)) == [] and run(svc, AT_7PM + timedelta(hours=3)) == []
    db.expire_all()
    note = db.scalar(select(Notification).where(Notification.template == "daily_report"))
    assert note.to_number == OWNER and note.status == "mock_sent"
    assert note.body == "CI Store 29Sep: 1 order, N16,400, 1 unpaid. Missed: Pizza x1. 1 callback due."
    rec = db.scalar(select(DailyReport))
    assert (rec.status, rec.attempts, rec.day, rec.body) == ("sent", 1, "2026-09-29", note.body) and rec.sent_at
    assert len(db.scalars(select(Notification).where(Notification.template == "daily_report")).all()) == 1


def test_next_day_gets_its_own_report(busy_day, app, db):
    svc = app.state.svc
    run(svc, AT_7PM)
    o = db.scalar(select(Order))
    o.confirmed_at = DAY + timedelta(days=1)
    db.commit()
    assert run(svc, AT_7PM + timedelta(days=1)) == ["CI Store"]
    assert {r.day for r in db.scalars(select(DailyReport))} == {"2026-09-29", "2026-09-30"}


def test_a_quiet_day_sends_nothing(app, db):
    assert run(app.state.svc, AT_7PM) == []
    assert db.scalar(select(DailyReport)).status == "skipped_empty"
    assert not db.scalars(select(Notification).where(Notification.template == "daily_report")).all()


def test_merchant_can_change_the_time_or_switch_it_off(busy_day, app, db):
    svc = app.state.svc
    busy_day.report_time = "20:30"
    db.commit()
    assert run(svc, AT_7PM) == []  # 19:00 Lagos is before 20:30
    assert run(svc, datetime(2026, 9, 29, 19, 30, tzinfo=UTC)) == ["CI Store"]
    db.query(DailyReport).delete()
    busy_day.report_enabled = False
    db.commit()
    assert run(svc, datetime(2026, 9, 29, 22, 0, tzinfo=UTC)) == []
    busy_day.report_enabled, busy_day.status = True, "paused"
    db.commit()
    assert run(svc, datetime(2026, 9, 29, 22, 0, tzinfo=UTC)) == []  # paused merchants get nothing


def test_report_goes_to_owners_only(busy_day, app, db):
    from sofa.models import MerchantUser

    db.add(MerchantUser(merchant_id=busy_day.id, name="Staff", phone="+2348077770000", role="staff", can_update_stock=True))
    db.commit()
    run(app.state.svc, AT_7PM)
    assert [n.to_number for n in db.scalars(select(Notification).where(Notification.template == "daily_report"))] == [OWNER]


def test_failed_send_is_retried_ten_minutes_later_up_to_three_times(busy_day, app, db, monkeypatch):
    svc = app.state.svc

    async def failing(to, message):
        return "failed", None

    monkeypatch.setattr(svc.sms, "send", failing)
    assert run(svc, AT_7PM) == []
    assert db.scalar(select(DailyReport)).status == "failed"
    assert run(svc, AT_7PM + timedelta(minutes=5)) == []  # too soon to retry
    db.expire_all()
    assert db.scalar(select(DailyReport)).attempts == 1
    run(svc, AT_7PM + timedelta(minutes=11))
    run(svc, AT_7PM + timedelta(minutes=22))
    run(svc, AT_7PM + timedelta(minutes=40))  # a 4th attempt is never made
    db.expire_all()
    assert db.scalar(select(DailyReport)).attempts == 3 and db.scalar(select(DailyReport)).status == "failed"

    # once SMS works again on a fresh day, it goes out
    async def working(to, message):
        return "sent", "id-1"

    monkeypatch.setattr(svc.sms, "send", working)
    o = db.scalar(select(Order))
    o.confirmed_at = DAY + timedelta(days=1)
    db.commit()
    assert run(svc, AT_7PM + timedelta(days=1)) == ["CI Store"]


def test_a_report_already_claimed_by_another_worker_is_not_sent_again(busy_day, app, db):
    day = reports.day_bounds(AT_7PM)[2]
    db.add(DailyReport(merchant_id=busy_day.id, day=day, status="sent", attempts=1))
    db.commit()
    assert run(app.state.svc, AT_7PM) == []
    assert not db.scalars(select(Notification).where(Notification.template == "daily_report")).all()


def test_merchant_without_an_owner_phone_is_recorded_not_retried(busy_day, app, db):
    from sofa.models import MerchantUser

    db.query(MerchantUser).delete()
    db.commit()
    assert run(app.state.svc, AT_7PM) == []
    assert db.scalar(select(DailyReport)).status == "no_recipient"


def test_scheduler_loop_survives_a_failing_tick_and_stops_when_cancelled(app, monkeypatch):
    svc = app.state.svc
    svc.settings.scheduler_interval_seconds = 0.01
    ticks = []

    async def flaky(_svc):
        ticks.append(1)
        if len(ticks) == 1:
            raise RuntimeError("database hiccup")
        return []

    monkeypatch.setattr(scheduler, "run_due_reports", flaky)

    async def scenario():
        task = asyncio.create_task(scheduler.loop(svc))
        await asyncio.sleep(0.15)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert len(ticks) >= 3  # kept ticking after the first one blew up


# ---- the owner asks by voice --------------------------------------------------------------------------------------------


def test_owner_hears_the_report_by_phone(call, db):
    call(SPEC_CONVERSATION)  # a real order today
    call(["Do you have pizza"], caller="+2348055550002")
    replies = call(["How did we do today?", "Yes"], caller=OWNER)
    heard = replies[1]
    assert heard.startswith("Today you have one order, 16,400 naira.")
    assert "One is waiting for payment." in heard
    assert "Callers also asked for Pizza, which you did not have." in heard
    assert heard.endswith("Anything else?")
    assert "callers are waiting" not in heard  # no handoff yet


def test_owner_report_pluralises_and_lists_callbacks(call, db):
    call(SPEC_CONVERSATION)
    call(["Let me talk to the owner"], caller="+2348055550002")
    call(["Let me talk to the owner"], caller="+2348055550003")
    heard = call(["how many orders today"], caller=OWNER)[1]
    assert "2 callers are waiting for you to call them back." in heard


def test_owner_report_on_an_empty_day_and_it_counts_as_answered(call, db):
    call(["How did we do today?"], caller=OWNER)
    reply = db.scalar(select(Call).order_by(Call.started_at.desc()))
    assert reply.outcome == "answered"
    assert call(["daily report"], caller=OWNER)[1] == "No orders yet today. Anything else?"


def test_owner_can_ask_for_the_report_then_still_update_stock(call, db):
    replies = call(["How did we do today?", "Add 20 cartons of Indomie Super Pack, price is 7500", "Yes"], caller=OWNER)
    assert "No orders yet today" in replies[1] and "Add 20 cartons of Indomie Super Pack" in replies[2]


def test_customers_cannot_get_the_report_even_if_the_model_says_so(call, app, monkeypatch):
    """The intent exists in the schema for every caller, so a customer's turn CAN come back as daily_report.
    The customer path must never reveal the shop's figures."""
    from sofa.clients.llm import TurnJSON

    async def always_report(system, transcript, alternatives=None):
        return TurnJSON(intent="daily_report"), {"intent": "daily_report"}

    monkeypatch.setattr(app.state.svc.llm, "parse_turn", always_report)
    replies = call(["How did we do today?", "and today's sales?"])
    for reply in replies[1:]:
        assert "Today you have" not in reply and "waiting for payment" not in reply
    assert "call you back" in replies[-1]  # two turns it cannot act on end in a handoff, as for any unknown intent


# ---- admin controls --------------------------------------------------------------------------------------------------------------


def test_admin_shows_preview_saves_settings_and_sends_a_sample(client, call, db):
    call(SPEC_CONVERSATION, caller="+2348000000001")  # a team test call: not in the 7 PM report, but in the preview
    login(client)
    mid = merchant_of(db).id
    page = client.get(f"/admin/merchants/{mid}").text
    assert "as it would be texted now" in page and "1 order, N16,400" in page and "of 160 characters" in page

    base = {"name": "CI Store", "category": "provisions", "default_language": "en", "payment_mode": "transfer_first", "status": "active"}
    bad = post(client, f"/admin/merchants/{mid}", {**base, "report_time": "7pm", "report_enabled": "on"}, page=f"/admin/merchants/{mid}")
    assert "report time must look like 19:00" in flash(client, bad)
    post(client, f"/admin/merchants/{mid}", {**base, "report_time": "20:15"}, page=f"/admin/merchants/{mid}")  # box unticked
    db.expire_all()
    m = merchant_of(db)
    assert (m.report_time, m.report_enabled) == ("20:15", False)

    r = post(client, f"/admin/merchants/{mid}/report/send", page=f"/admin/merchants/{mid}")
    assert "Sample report sent to 1 owner phone(s)" in flash(client, r) and "mock" in flash(client, r)
    db.expire_all()
    note = db.scalar(select(Notification).where(Notification.template == "daily_report"))
    assert note.to_number == OWNER and "1 order" in note.body
    assert not db.scalars(select(DailyReport)).all()  # a sample never uses up the day's real report
