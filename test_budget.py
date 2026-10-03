"""Run: python test_budget.py  (uses throwaway DBs, never your real one)"""
import copy
import itertools
import os
import sqlite3
import tempfile
import time
from types import SimpleNamespace
from typing import Any

TMP = tempfile.mkdtemp()
os.environ["BUDGET_DB"] = os.path.join(TMP, "t.db")
# never let a developer .env point the tests at a real cloud database or turn on the password gate
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["APP_PASSWORD"] = ""
os.environ["ADMIN_USER"] = "admin"
import main  # noqa: E402
import tursodb  # noqa: E402

T = main.cur()


_db_ids = itertools.count()


def fresh():
    main.DB = os.path.join(TMP, f"t{next(_db_ids)}.db")
    main.init()


def debt(i, person, direction, amount, rate=0, monthly=0):
    return dict(id=i, person=person, direction=direction, amount=amount, rate=rate, monthly=monthly, due="", note="")


def row(month, **kw):
    return {"month": month, "salary": None, "ef": None, "sip": None, "extra_income": 0, "skip_ef": 0, "skip_sip": 0,
            "skip_debt": 0, "note": "", **kw}


def test_seed_and_basics():
    fresh()
    b = main.load()
    v = main.month_view(b, T)
    assert (v["spending"], v["left"], v["buffer"], v["ef_target"]) == (43040, 55460, 12960, 258240), v
    assert main.month_view(b, main.addm(T, -1))["income"] == 0  # no data before the first month
    assert main.inr(125000) == "₹1,25,000" and main.inr(-5000) == "-₹5,000"
    assert main.addm("2026-12", 1) == "2027-01" and main.addm("2026-01", -1) == "2025-12" and main.diffm("2026-10", "2027-01") == 3


def test_month_semantics():
    fresh()
    b = main.load()
    nxt = main.addm(T, 1)
    # one-time expense only hits its own month
    b["expenses"].append(dict(id=99, name="Goa trip", amount=20000, type="variable", priority="important",
                              recurring=False, start=nxt, until=None))
    assert main.month_view(b, nxt)["spending"] == 63040 and main.month_view(b, main.addm(T, 2))["spending"] == 43040
    # salary change carries forward but does not rewrite the past
    b["months"].append(row(nxt, salary=110000))
    assert main.month_view(b, T)["salary"] == 98500 and main.month_view(b, main.addm(T, 5))["salary"] == 110000
    # skip + extra income are month-only
    b["months"].append(row(main.addm(T, 2), skip_ef=1, skip_sip=1, extra_income=5000))
    v = main.month_view(b, main.addm(T, 2))
    assert (v["ef"], v["sip"], v["income"]) == (0, 0, 115000) and main.month_view(b, main.addm(T, 3))["ef"] == 10000
    # money received back from people counts as income that month
    b["payments"].append(dict(id=1, debt_id=1, person="x", direction="lent", month=T, amount=3000))
    assert main.month_view(b, T)["income"] == 101500


def test_history_preserving_edit():
    fresh()
    m2 = main.addm(T, 2)
    rent = next(e for e in main.load()["expenses"] if e["name"] == "Rent")
    ch = []
    assert main.run_tool("update_expense", {"id": rent["id"], "amount": 20000}, ch, m2) == "ok"
    b = main.load()
    assert main.month_view(b, T)["spending"] == 43040 and main.month_view(b, main.addm(T, 1))["spending"] == 43040
    assert main.month_view(b, m2)["spending"] == 46040 and main.month_view(b, main.addm(T, 6))["spending"] == 46040
    main.run_tool("remove_expense", {"id": rent["id"]}, ch, main.addm(T, 4))  # stops from month 4 on
    b = main.load()
    assert main.month_view(b, main.addm(T, 3))["spending"] == 46040 and main.month_view(b, main.addm(T, 4))["spending"] == 26040
    main.run_tool("add_expense", {"name": "Gift", "amount": 2000, "repeats_monthly": False}, ch, T)
    b = main.load()  # the one-time gift hits only its own month
    assert main.month_view(b, T)["spending"] == 45040 and main.month_view(b, main.addm(T, 1))["spending"] == 43040


def test_repay_sim():
    fresh()
    b = main.load()
    b["expenses"] = [e for e in b["expenses"] if e["start"] <= T and e["recurring"]][:9]
    b["settings"].update(debt_pct=100, debt_strategy="avalanche")
    b["debts"] = [debt(1, "A", "owe", 100000)]
    base = main.repay_sim(b)
    assert base["months"] == 8 and base["sched"][0]["total"] == 12960, base["months"]
    # 5,000/mo coming back for 2 months speeds it up
    b["debts"].append(debt(2, "B", "lent", 10000, monthly=5000))
    p = main.repay_sim(b)
    assert p["months"] == 7 and p["sched"][0]["total"] == 17960
    b["debts"].pop()
    # cost of skipping is the same whether or not the flag is already set
    assert main.skip_impact(b, T)["delay_months"] == 1
    # skipping this month's payment pays nothing now and delays debt-free by one month
    b["months"][0]["skip_debt"] = 1  # one row per month, like the client
    s = main.repay_sim(b)
    assert s["sched"][0]["total"] == 0 and s["months"] == 9
    imp = main.skip_impact(b, T, s)
    assert imp["delay_months"] == 1 and imp["allocation"] == 12960
    assert main.planned_debt(b, T, s) == 0
    # a payment already made this month is not suggested again
    b["months"][0]["skip_debt"] = 0
    b["payments"].append(dict(id=1, debt_id=1, person="A", direction="owe", month=T, amount=5000))
    assert round(main.repay_sim(b)["sched"][0]["total"]) == 7960
    # avalanche pays the 24% debt first, snowball the smaller balance
    b["payments"].clear()
    b["debts"] = [debt(1, "low", "owe", 10000, 12), debt(2, "high", "owe", 50000, 24)]
    assert main.repay_sim(b)["order"][0]["person"] == "high"
    b["settings"]["debt_strategy"] = "snowball"
    assert main.repay_sim(b)["order"][0]["person"] == "low"
    # minimums bigger than the fund -> shortfall
    b["settings"].update(debt_pct=10, debt_strategy="avalanche")
    b["debts"] = [debt(1, "A", "owe", 100000, 0, 5000)]
    assert main.repay_sim(b)["shortfall"] > 0


def test_tracking_starts_in_first_salary_month():
    fresh()
    b = main.load()
    nxt = main.addm(T, 1)
    b["months"][0]["month"] = nxt
    for e in b["expenses"]:
        e["start"] = nxt
    b["debts"] = [debt(1, "A", "owe", 100000)]
    assert main.data_start(b) == nxt and main.plan_start(b) == nxt
    assert main.month_view(b, T)["income"] == 0 and main.month_view(b, T)["spending"] == 0
    sim = main.repay_sim(b)
    assert sim["sched"][0]["month"] == nxt and sim["months"] == 8
    assert main.planned_debt(b, T, sim) == 0 and main.planned_debt(b, nxt, sim) == 12960


def _fake_hrana():
    """Stand-in for Turso's HTTP API, backed by in-memory sqlite, so app code runs through the real client."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    enc = lambda v: ({"type": "null"} if v is None else {"type": "integer", "value": str(v)} if isinstance(v, int)
                     else {"type": "float", "value": v} if isinstance(v, float) else {"type": "text", "value": v})
    dec = lambda v: None if v["type"] == "null" else int(v["value"]) if v["type"] == "integer" else v["value"]

    def run(st):
        params = ({a["name"].lstrip(":"): dec(a["value"]) for a in st["named_args"]} if "named_args" in st
                  else [dec(a) for a in st.get("args", [])])
        cur = conn.execute(st["sql"], params)
        return {"cols": [{"name": d[0]} for d in cur.description or []],
                "rows": [[enc(v) for v in r] for r in cur.fetchall()], "affected_row_count": cur.rowcount}

    def post(self, requests):
        out = []
        for r in requests:
            if r["type"] == "execute":
                out.append({"type": "execute", "result": run(r["stmt"])})
                continue
            res, errs = [], []

            def ok(c):
                return (not ok(c["cond"])) if c["type"] == "not" else errs[c["step"]] is None and res[c["step"]] is not None
            for st in r["batch"]["steps"]:
                if st.get("condition") and not ok(st["condition"]):
                    res.append(None)
                    errs.append(None)
                    continue
                try:
                    res.append(run(st["stmt"]))
                    errs.append(None)
                except sqlite3.Error as e:
                    res.append(None)
                    errs.append({"message": str(e)})
            out.append({"type": "batch", "result": {"step_results": res, "step_errors": errs}})
        return out
    return post


def test_turso_client_runs_the_app():
    real_post, real_url = tursodb.Remote._post, main.TURSO_URL
    tursodb.Remote._post, main.TURSO_URL = _fake_hrana(), "libsql://fake.example"
    try:
        main.init()
        b = main.load()
        v = main.month_view(b, T)
        assert (v["spending"], v["buffer"]) == (43040, 12960)
        b["debts"] = [debt(None, "papa", "owe", 194000)]
        b["notes"] = [dict(id=None, text="rule")]
        main.save(b)
        assert main.load()["debts"][0]["person"] == "papa" and main.load()["notes"][0]["text"] == "rule"
        bad = main.load()  # a duplicate id makes one insert fail: nothing may change
        bad["expenses"].append(dict(bad["expenses"][0]))
        try:
            main.save(bad)
            raise AssertionError("expected the save to fail")
        except RuntimeError:
            pass
        after = main.load()
        assert len(after["expenses"]) == 9 and after["debts"][0]["person"] == "papa"
        ch = []
        rent = next(e for e in after["expenses"] if e["name"] == "Rent")
        assert main.run_tool("update_expense", {"id": rent["id"], "amount": 20000}, ch, main.addm(T, 2)) == "ok"
        assert main.run_tool("record_payment", {"id": after["debts"][0]["id"], "amount": 4000}, ch, T) == "ok"
        end = main.load()
        assert main.month_view(end, T)["spending"] == 43040 and main.month_view(end, main.addm(T, 2))["spending"] == 46040
        assert end["debts"][0]["amount"] == 190000 and end["payments"][0]["amount"] == 4000
    finally:
        tursodb.Remote._post, main.TURSO_URL = real_post, real_url


def test_lockouts_hit_the_attacker_not_the_family():
    main.FAILS.clear()
    for _ in range(10):
        main._fail("1.1.1.1", "mom")
    assert main._locked("1.1.1.1", "dad")  # the attacker's address is locked
    assert main._locked("9.9.9.9", "mom")  # and so is the targeted account, from any address
    assert not main._locked("9.9.9.9", "dad") and not main._locked("9.9.9.9", main.ADMIN_USER)
    main.FAILS.clear()
    for i in range(200):  # a spread-out attack on many accounts never produces a global lock
        main._fail(f"2.2.{i}.1", f"user{i}")
    assert not main._locked("3.3.3.3", "mom") and not main._locked("3.3.3.3", main.ADMIN_USER)
    main.FAILS.clear()
    for _ in range(50):  # nobody can lock the owner out by guessing the owner's password
        main._fail("4.4.4.4", main.ADMIN_USER)
    assert not main._locked("5.5.5.5", main.ADMIN_USER) and main._locked("4.4.4.4", main.ADMIN_USER)
    main.FAILS.clear()


def test_invite_code_signup():
    fresh()
    assert main.invite_code() == ""
    assert main.try_signup("sis", "family-secret-9", "x") == (None, "Sign-up is closed. Ask the owner for an account.")
    main.set_invite(True)
    code = main.invite_code()
    assert len(code) == 10 and main.invite_ok(code.upper() + " ") and not main.invite_ok("wrong") and not main.invite_ok("")
    assert main.try_signup("sis", "family-secret-9", "wrong") == (None, "That invite code is not right.")
    assert main.try_signup("sis", "short", code)[1] == "Password must be at least 10 characters."
    real_pw, main.APP_PASSWORD = main.APP_PASSWORD, "owner-pass"
    try:
        uid, err = main.try_signup("Sis", "family-secret-9", code)
        assert err is None and uid and main.authenticate("sis", "family-secret-9") == uid
        assert main.try_signup("sis", "family-secret-9", code)[1] == "That username is already taken."
        assert main.load(uid)["expenses"] == []  # a fresh private budget
        main.set_invite(True)  # a new code makes the old one useless
        assert main.invite_code() != code and not main.invite_ok(code)
        main.set_invite(False)
        assert main.invite_code() == "" and not main.invite_ok(main.invite_code())
        main.set_invite(True)
        real_max, main.MAX_USERS = main.MAX_USERS, 2  # owner + sis already fill it
        assert main.try_signup("bro", "tiffin-box-1234", main.invite_code()) == (None, "This site has reached its account limit.")
        main.MAX_USERS = real_max
    finally:
        main.APP_PASSWORD = real_pw


def test_password_rules_and_invite_code_limit():
    fresh()
    assert main.create_user("mom", "short") == "Password must be at least 10 characters."
    assert main.create_user("mom", "xx-MOM-xx-1234") == "Password must not contain the username."
    assert main.create_user("mom", "a-good-secret-1") is None
    uid = main.authenticate("mom", "a-good-secret-1") if main.APP_PASSWORD else None
    with main.db() as c:
        uid = c.execute("select id from users where username='mom'").fetchone()[0]
    assert main.reset_password(uid, "mom-1234567") == "Password must not contain the username."
    assert main.reset_password(uid, "another-secret-2") is None
    main.FAILS.clear()
    for _ in range(29):
        main.FAILS.setdefault("signup", []).append(time.time())
    assert not main._code_locked()
    main.FAILS["signup"].append(time.time())
    assert main._code_locked() and not main._locked("1.2.3.4", "mom")  # sign-up pauses; logins are unaffected
    main.FAILS.clear()


def test_client_address_behind_render():
    def req(**headers) -> Any:
        return SimpleNamespace(headers={k.replace("_", "-"): v for k, v in headers.items()}, client=SimpleNamespace(host="10.0.0.9"))
    assert main._ip(req(cf_connecting_ip="203.0.113.7")) == "203.0.113.7"  # Cloudflare's own header wins
    assert main._ip(req(x_forwarded_for="203.0.113.7, 172.70.1.1")) == "203.0.113.7"  # client, then Render's proxy
    assert main._ip(req(x_forwarded_for="6.6.6.6, 203.0.113.7, 172.70.1.1")) == "203.0.113.7"  # a faked left entry is ignored
    assert main._ip(req(x_forwarded_for="1.2.3.4")) == "1.2.3.4"  # a single entry (no proxy chain)
    assert main._ip(req()) == "10.0.0.9"  # nothing but the connection


def test_forecast():
    fresh()
    b = main.load()
    b["settings"].update(ret=0, hike=0, infl=0)
    assert main.forecast(b, T, 12)["ef"] == 120000 and round(main.forecast(b, T, 12)["inv"]) == 390000
    f = main.forecast(b, T, 36)
    assert f["ef_full_month"] == 26 and round(f["inv"]) == 1271760, f


def test_snapshot_and_roundtrip():
    fresh()
    b = main.load()
    b["debts"] = [debt(None, "papa", "owe", 194000)]
    b["notes"] = [dict(id=None, text="All free buffer goes to papa")]
    b["settings"]["debt_pct"] = 100
    main.save(b)
    b = main.load()
    snap = main.snapshot(b, T)
    # the bug that started this: with 100% earmarked, nothing is free to spend on a phone
    assert snap["month"]["SPENDABLE_AFTER_DEBT_PLAN"] == 0, snap["month"]
    assert snap["standing_rules"][0]["rule"].startswith("All free buffer")
    b["months"][0]["skip_debt"] = 1
    assert main.snapshot(b, T)["month"]["SPENDABLE_AFTER_DEBT_PLAN"] == 12960  # skipping frees it


def _v2_db(path):
    c = sqlite3.connect(path)
    c.executescript("""
      create table settings(id integer primary key check(id=1), ef_saved real, ef_months real, ret real, hike real,
        infl real, debt_pct real, debt_strategy text);
      insert into settings values(1,0,6,0,8,5,50,'avalanche');
      create table months(month text primary key, salary real, ef real, sip real, extra_income real default 0,
        skip_ef integer default 0, skip_sip integer default 0, skip_debt integer default 0, note text default '');
      insert into months(month,salary,ef,sip) values('2026-11',98500,30000,0);
      create table expenses(id integer primary key, name text, amount real, type text, priority text default 'essential',
        recurring integer default 1, start text, until text);
      insert into expenses(id,name,amount,type,priority,start) values(1,'Rent',17000,'fixed','essential','2026-11'),
        (2,'Cigarettes',3000,'variable','optional','2026-11');
      create table debts(id integer primary key, person text, direction text, amount real, rate real, monthly real, due text, note text);
      insert into debts values(1,'papa','owe',194000,0,0,'2028-01-01','');
      create table payments(id integer primary key, debt_id integer, person text, direction text, month text, amount real);
      create table notes(id integer primary key, text text);
      insert into notes values(1,'all buffer to papa');
    """)
    c.commit()
    c.close()


def _check_owner_data(b):
    assert b["settings"]["debt_pct"] == 50 and b["debts"][0]["amount"] == 194000 and b["notes"][0]["text"] == "all buffer to papa"
    v = main.month_view(b, "2026-11")
    assert (v["salary"], v["ef"], v["sip"], v["spending"]) == (98500, 30000, 0, 20000)


def test_migration_from_v2_keeps_the_owner_data():
    path = os.path.join(TMP, "v2.db")
    _v2_db(path)
    main.DB = path
    main.init()
    _check_owner_data(main.load())
    assert any(f.startswith("v2.db.bak-v2-") for f in os.listdir(TMP))
    main.init()  # idempotent
    main.save(copy.deepcopy(main.load()))
    _check_owner_data(main.load())


def test_interrupted_migration_resumes():
    path = os.path.join(TMP, "v2b.db")
    _v2_db(path)
    c = sqlite3.connect(path)
    c.execute("alter table expenses rename to expenses_v2")  # crashed right after the first rename
    c.commit()
    c.close()
    main.DB = path
    main.init()
    b = main.load()
    _check_owner_data(b)
    assert len(b["expenses"]) == 2


def test_users_have_private_budgets():
    fresh()
    assert main.create_user("mom", "kitchen-table-77") is None
    assert main.create_user("Mom", "another-pass") == "That username is already taken."
    assert "at least 10" in (main.create_user("dad", "short") or "")
    assert "3-30" in (main.create_user("A!", "longenough1") or "")
    real_pw, main.APP_PASSWORD = main.APP_PASSWORD, "owner-pass"
    try:
        assert main.authenticate("admin", "owner-pass") == 1 and main.authenticate(" ADMIN ", "owner-pass") == 1
        assert main.authenticate("admin", "wrong") is None and main.authenticate("ghost", "x") is None
        uid = main.authenticate("mom", "kitchen-table-77")
        assert uid and uid != 1 and main.authenticate("mom", "nope") is None
        assert main.authenticate("admin", "kitchen-table-77") is None and main.authenticate("mom", "owner-pass") is None
        # separate data, even with colliding ids
        mom = main.load(uid)
        assert mom["expenses"] == [] and mom["debts"] == []
        mom["months"].append(row(T, salary=50000))
        mom["expenses"].append(dict(id=1, name="Rent", amount=9000, type="fixed", priority="essential", recurring=True, start=T, until=None))
        main.save(mom, uid)
        owner = main.load()
        assert len(owner["expenses"]) == 9 and main.month_view(owner, T)["spending"] == 43040
        assert main.month_view(main.load(uid), T)["spending"] == 9000
        # assistant tools only touch the caller's own rows
        ch: list[str] = []
        main.run_tool("add_expense", {"name": "Tea", "amount": 50}, ch, T, uid)
        assert len(main.load(uid)["expenses"]) == 2 and len(main.load()["expenses"]) == 9
        assert main.run_tool("update_expense", {"id": 1, "amount": 1}, ch, T, uid) == "ok"
        assert main.load()["expenses"][0]["amount"] == 17000 and main.load(uid)["expenses"][0]["amount"] == 1
        # sessions
        def stub(tok) -> Any:  # just enough of a Request for the cookie checks
            return SimpleNamespace(cookies={"bd_session": tok})
        req = stub(main._session_token(uid, int(time.time()) + 100))
        assert main._session_uid(req) == uid
        good = req.cookies["bd_session"]
        assert main._session_uid(stub(good[:-1] + ("0" if good[-1] != "0" else "1"))) is None  # tampered
        assert main._session_uid(stub(main._session_token(uid, int(time.time()) - 5))) is None  # expired
        assert main._session_uid(stub("1." + good.split(".", 1)[1])) is None  # cannot swap in the owner's id
        assert main._session_uid(stub("")) is None and main._session_uid(stub("a.b.c")) is None
        # a password reset ends the old session; the new password works
        assert main.reset_password(uid, "morning-chai-88") is None
        assert main._session_uid(req) is None
        assert main.authenticate("mom", "kitchen-table-77") is None and main.authenticate("mom", "morning-chai-88") == uid
        assert "APP_PASSWORD" in (main.reset_password(1, "whatever123") or "")
        # deleting removes the person and every row of theirs
        token = main._session_token(uid, int(time.time()) + 100)
        main.delete_user(uid)
        assert main._session_uid(stub(token)) is None and main.authenticate("mom", "morning-chai-88") is None
        with main.db() as c:
            assert all(c.execute(f"select count(*) from {t} where user_id=?", (uid,)).fetchone()[0] == 0 for t, _ in main.TABLES)
        assert len(main.load()["expenses"]) == 9  # the owner is untouched
    finally:
        main.APP_PASSWORD = real_pw


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        fn()
print("ok")
