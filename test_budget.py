"""Run: python test_budget.py  (uses throwaway DBs, never your real one)"""
import copy
import os
import sqlite3
import tempfile

TMP = tempfile.mkdtemp()
os.environ["BUDGET_DB"] = os.path.join(TMP, "t.db")
# never let a developer .env point the tests at a real cloud database or turn on the password gate
os.environ["TURSO_DATABASE_URL"] = ""
os.environ["APP_PASSWORD"] = ""
import main  # noqa: E402
import tursodb  # noqa: E402

T = main.cur()


def fresh():
    main.DB = os.path.join(TMP, f"t{fresh.n}.db")
    fresh.n += 1
    main.init()


fresh.n = 0


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
                    res.append(None), errs.append(None)
                    continue
                try:
                    res.append(run(st["stmt"])), errs.append(None)
                except sqlite3.Error as e:
                    res.append(None), errs.append({"message": str(e)})
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


def test_migration_from_v1():
    path = os.path.join(TMP, "v1.db")
    c = sqlite3.connect(path)
    c.executescript("""
      create table settings(id integer primary key, salary real, ef real, sip real, ef_saved real, ef_months real,
        ret real, hike real, infl real, debt_pct real, debt_strategy text);
      insert into settings values(1,98500,30000,0,0,6,0,8,5,50,'avalanche');
      create table expenses(id integer primary key, name text, amount real, type text);
      insert into expenses values(1,'Rent',17000,'fixed'),(2,'Cigarettes',3000,'variable');
      create table debts(id integer primary key, person text, direction text, amount real, rate real, monthly real, due text, note text);
      insert into debts values(1,'papa','owe',194000,0,0,'2028-01-01','');
    """)
    c.commit()
    c.close()
    main.DB = path
    main.init()
    b = main.load()
    assert b["settings"]["debt_pct"] == 50 and b["debts"][0]["amount"] == 194000
    v = main.month_view(b, T)
    assert (v["salary"], v["ef"], v["sip"], v["spending"]) == (98500, 30000, 0, 20000)
    assert {e["name"]: e["priority"] for e in b["expenses"]} == {"Rent": "essential", "Cigarettes": "optional"}
    assert any(f.startswith("v1.db.bak-") for f in os.listdir(TMP))
    main.init()  # idempotent on an already-migrated db
    main.save(copy.deepcopy(main.load()))


for name, fn in list(globals().items()):
    if name.startswith("test_"):
        fn()
print("ok")
