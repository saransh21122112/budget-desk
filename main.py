import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import secrets
import time
from urllib.parse import parse_qs
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import openai
import tursodb
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")
DB = os.getenv("BUDGET_DB", str(HERE / "budget.db"))
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "")  # set on a host with no persistent disk; otherwise local SQLite file

SETTINGS = ["ef_saved", "ef_months", "ret", "hike", "infl", "debt_pct", "debt_strategy"]
SETTINGS_DEFAULTS = dict(ef_saved=0, ef_months=6, ret=12, hike=8, infl=5, debt_pct=100, debt_strategy="avalanche")
MONTH_COLS = ["month", "salary", "ef", "sip", "extra_income", "skip_ef", "skip_sip", "skip_debt", "note"]
EXP_COLS = ["id", "name", "amount", "type", "priority", "recurring", "start", "until"]
DEBT_COLS = ["id", "person", "direction", "amount", "rate", "monthly", "due", "note"]
PAY_COLS = ["id", "debt_id", "person", "direction", "month", "amount"]
PRIORITIES = ("essential", "important", "optional")
SEED_EXPENSES = [("Rent", 17000, "fixed", "essential"), ("Bike EMI", 6340, "fixed", "essential"),
                 ("Ration", 5000, "fixed", "essential"), ("LIC 1", 3900, "fixed", "essential"),
                 ("Cigarettes", 3000, "variable", "optional"), ("Electricity", 2500, "fixed", "essential"),
                 ("Maid", 2500, "fixed", "important"), ("LIC 2", 1800, "fixed", "essential"),
                 ("Bike petrol", 1000, "variable", "essential")]


TZ = ZoneInfo(os.getenv("APP_TZ", "Asia/Kolkata"))  # a UTC server would be a month behind for 5.5h each month


def today():
    return datetime.now(TZ).date()


def cur():
    return today().strftime("%Y-%m")


# ---------- months ----------
def addm(M, k):
    y, m = map(int, M.split("-"))
    t = y * 12 + m - 1 + k
    return f"{t // 12}-{t % 12 + 1:02d}"


def diffm(a, b):
    (ya, ma), (yb, mb) = (map(int, a.split("-")), map(int, b.split("-")))
    return (yb - ya) * 12 + mb - ma


# ---------- storage ----------
@contextmanager
def db():
    if TURSO_URL:
        yield tursodb.Remote(TURSO_URL, os.getenv("TURSO_AUTH_TOKEN", ""))
        return
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


SCHEMA = """
create table if not exists settings(id integer primary key check(id=1), ef_saved real, ef_months real,
  ret real, hike real, infl real, debt_pct real, debt_strategy text);
create table if not exists months(month text primary key, salary real, ef real, sip real,
  extra_income real default 0, skip_ef integer default 0, skip_sip integer default 0,
  skip_debt integer default 0, note text default '');
create table if not exists expenses(id integer primary key, name text, amount real, type text,
  priority text default 'essential', recurring integer default 1, start text, until text);
create table if not exists debts(id integer primary key, person text, direction text, amount real,
  rate real, monthly real, due text, note text);
create table if not exists payments(id integer primary key, debt_id integer, person text, direction text,
  month text, amount real);
create table if not exists notes(id integer primary key, text text);
"""


def init():
    with db() as c:
        old = {r[1] for r in c.execute("pragma table_info(settings)")}
        if "salary" in old:  # v1 (single-month) database: back it up, then migrate in place
            shutil.copy(DB, f"{DB}.bak-{int(time.time())}")
            v1 = dict(c.execute("select * from settings").fetchone())
            c.execute("alter table settings rename to settings_v1")
            for col, decl in [("priority", "text default 'essential'"), ("recurring", "integer default 1"),
                              ("start", "text"), ("until", "text")]:
                if col not in {r[1] for r in c.execute("pragma table_info(expenses)")}:
                    c.execute(f"alter table expenses add column {col} {decl}")
            c.executescript(SCHEMA)
            c.execute("insert into settings(id," + ",".join(SETTINGS) + ") values(1," + ",".join("?" * len(SETTINGS)) + ")",
                      [v1[k] for k in SETTINGS])
            c.execute("insert into months(month,salary,ef,sip) values(?,?,?,?)", (cur(), v1["salary"], v1["ef"], v1["sip"]))
            c.execute("update expenses set start=? where start is null", (cur(),))
            c.execute("update expenses set priority=case type when 'variable' then 'optional' else 'essential' end")
            c.execute("drop table settings_v1")
            return
        c.executescript(SCHEMA)
        if not c.execute("select 1 from settings").fetchone():
            c.execute("insert into settings(id," + ",".join(SETTINGS) + ") values(1," + ",".join("?" * len(SETTINGS)) + ")",
                      [SETTINGS_DEFAULTS[k] for k in SETTINGS])
            c.execute("insert into months(month,salary,ef,sip) values(?,98500,10000,32500)", (cur(),))
            c.executemany("insert into expenses(name,amount,type,priority,recurring,start) values(?,?,?,?,1,?)",
                          [(*e, cur()) for e in SEED_EXPENSES])


def load():
    with db() as c:
        rows = lambda q: [dict(r) for r in c.execute(q)]
        s = dict(c.execute("select * from settings").fetchone())
        s.pop("id")
        return {"settings": s, "months": rows("select * from months order by month"),
                "expenses": rows("select * from expenses order by id"), "debts": rows("select * from debts order by id"),
                "payments": rows("select * from payments order by id"), "notes": rows("select * from notes order by id")}


def save(b):
    ins = lambda t, cols: f"insert into {t}(" + ",".join(cols) + ") values(" + ",".join(":" + k for k in cols) + ")"
    stmts = [("update settings set " + ",".join(f"{k}=?" for k in SETTINGS) + " where id=1", [b["settings"][k] for k in SETTINGS])]
    for t, cols in [("months", MONTH_COLS), ("expenses", EXP_COLS), ("debts", DEBT_COLS),
                    ("payments", PAY_COLS), ("notes", ["id", "text"])]:
        stmts.append((f"delete from {t}", ()))
        stmts += [(ins(t, cols), row) for row in b[t]]
    with db() as c:
        if hasattr(c, "atomic"):  # Turso: one transaction, so a failed save never leaves a half-empty budget
            c.atomic(stmts)
        else:
            for sql, params in stmts:
                c.execute(sql, params)


# ---------- shared calculations (the frontend mirrors these in JS) ----------
def inr(n):
    n = round(n)
    s = str(abs(n))
    if len(s) > 3:
        s = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", s[:-3]) + "," + s[-3:]
    return ("-" if n < 0 else "") + "₹" + s


def eff(b, key, M):
    """Standing value (salary / ef / sip): latest month <= M that set it. Months before any data are 0."""
    best = None
    for r in b["months"]:
        if r["month"] <= M and r.get(key) is not None and (best is None or r["month"] > best["month"]):
            best = r
    return best[key] if best else 0


def active(b, M):
    def on(e):
        if e["recurring"]:
            return e["start"] <= M and (not e.get("until") or M <= e["until"])
        return e["start"] == M
    return [e for e in b["expenses"] if on(e)]


def paid(b, M, direction):
    return sum(p["amount"] for p in b["payments"] if p["month"] == M and p["direction"] == direction)


def data_start(b):
    """First month with a salary set; months before it are empty."""
    ms = [r["month"] for r in b["months"] if r.get("salary") is not None]
    return min(ms) if ms else cur()


def plan_start(b):
    return max(cur(), data_start(b))


def month_view(b, M):
    row = next((r for r in b["months"] if r["month"] == M), {})
    ex = active(b, M)
    spending = sum(e["amount"] for e in ex)
    salary, ef0, sip0 = eff(b, "salary", M), eff(b, "ef", M), eff(b, "sip", M)
    ef, sip = (0 if row.get("skip_ef") else ef0), (0 if row.get("skip_sip") else sip0)
    income = salary + (row.get("extra_income") or 0) + paid(b, M, "lent")  # money coming back from people counts
    left = income - spending
    return {"month": M, "salary": salary, "income": income, "spending": spending, "left": left, "ef": ef, "sip": sip,
            "ef0": ef0, "sip0": sip0, "buffer": left - ef - sip, "ef_target": spending * b["settings"]["ef_months"],
            "savings_rate": (ef + sip) / salary if salary else 0,
            "by_priority": {p: sum(e["amount"] for e in ex if e["priority"] == p) for p in PRIORITIES},
            "skip_ef": bool(row.get("skip_ef")), "skip_sip": bool(row.get("skip_sip")),
            "skip_debt": bool(row.get("skip_debt")), "extra_income": row.get("extra_income") or 0,
            "note": row.get("note") or "", "expenses": ex}


def forecast(b, M, months):
    s, v = b["settings"], month_view(b, M)
    r = s["ret"] / 1200
    sal, sp, sip, ef_c, ef, inv = v["salary"], v["spending"], v["sip0"], v["ef0"], s["ef_saved"], 0.0
    target = sp * s["ef_months"]
    full = 0 if ef >= target - 1e-6 else None
    for m in range(1, months + 1):
        inv *= 1 + r
        to_ef = min(ef_c, max(0, target - ef))
        ef += to_ef
        inv += sip + ef_c - to_ef  # overflow of the EF contribution goes to investments
        if full is None and ef >= target - 1e-6:
            full = m
        if m % 12 == 0:
            ns, nsp = sal * (1 + s["hike"] / 100), sp * (1 + s["infl"] / 100)
            sip = max(0, sip + (ns - sal) - (nsp - sp))  # extra salary after inflation goes to SIP
            sal, sp, target = ns, nsp, nsp * s["ef_months"]
    return {"ef": ef, "inv": inv, "total": ef + inv, "ef_full_month": full, "salary": sal}


def repay_sim(b, unskip=None, skip=None, extra_paid=0.0):
    """Month-by-month payoff from the current month using EACH month's own buffer
    (one-time expenses, extra income and skip flags included). Minimums first, then by strategy.
    unskip=M ignores that month's skip flag, skip=M pretends it is skipped; extra_paid pretends that much was already paid."""
    s, T = b["settings"], plan_start(b)
    owe = [dict(id=d["id"], person=d["person"], bal=d["amount"], rate=d["rate"], min=d["monthly"],
                paid=None, interest=0.0, pay1=0.0)
           for d in b["debts"] if d["direction"] == "owe" and d["amount"] > 0.005]
    lent = [dict(bal=d["amount"], exp=d["monthly"]) for d in b["debts"]
            if d["direction"] == "lent" and d["amount"] > 0.005]

    def order():
        live = [x for x in owe if x["bal"] > 0.005]
        key = (lambda x: x["bal"]) if s["debt_strategy"] == "snowball" else (lambda x: (-x["rate"], x["bal"]))
        return sorted(live, key=key)

    rows = [x["id"] for x in order()]
    for x in order():
        a = min(extra_paid, x["bal"])
        x["bal"] -= a
        extra_paid -= a
        if x["bal"] <= 0.005:
            x["paid"] = 0
    sched, shortfall, mins = [], 0.0, sum(x["min"] for x in owe)
    for k in range(600):
        if not any(x["bal"] > 0.005 for x in owe):
            break
        M = addm(T, k)
        v = month_view(b, M)
        for x in owe:
            if x["bal"] > 0.005:
                i = x["bal"] * x["rate"] / 1200
                x["bal"] += i
                x["interest"] += i
        inflow = sum(min(l["exp"], l["bal"]) for l in lent)
        if k == 0:
            inflow = max(0, inflow - paid(b, T, "lent"))  # some already came in this month
        left_in = inflow
        for l in lent:
            g = min(min(l["exp"], l["bal"]), left_in)
            l["bal"] -= g
            left_in -= g
        skipped = (v["skip_debt"] and M != unskip) or M == skip
        cap = 0.0 if skipped else max(0, v["buffer"]) * s["debt_pct"] / 100 + inflow
        if k == 0:
            cap = max(0, cap - paid(b, T, "owe"))  # already paid this month
        ord_ = order()
        if k == 0:
            shortfall = max(0, sum(min(x["min"], x["bal"]) for x in ord_) - cap) if not skipped else 0
        pay = {}
        for want in (lambda x: x["min"], lambda x: cap):  # minimums, then everything left
            for x in ord_:
                a = min(want(x), x["bal"], cap)
                if a > 0:
                    x["bal"] -= a
                    cap -= a
                    pay[x["id"]] = pay.get(x["id"], 0) + a
        for x in owe:
            if x["bal"] <= 0.005 and x["paid"] is None:
                x["paid"] = k + 1
        sched.append({"month": M, "skipped": skipped, "pay": pay, "total": sum(pay.values())})
    by_id = {x["id"]: x for x in owe}
    months = None if any(x["paid"] is None for x in owe) else max([x["paid"] for x in owe], default=0)
    first = sched[0]["pay"] if sched else {}
    return {"sched": sched, "months": months, "interest": sum(x["interest"] for x in owe), "shortfall": shortfall,
            "mins": mins, "order": [{"id": i, "person": by_id[i]["person"], "pay_now": round(first.get(i, 0)),
                                     "clears_in_months": by_id[i]["paid"]} for i in rows],
            "owe_total": sum(d["amount"] for d in b["debts"] if d["direction"] == "owe"),
            "lent_total": sum(d["amount"] for d in b["debts"] if d["direction"] == "lent")}


def planned_debt(b, M, sim=None):
    """What goes to debts in month M: actual payments so far, plus what the plan still wants this month."""
    p = paid(b, M, "owe")
    S = plan_start(b)
    if M < S:
        return p
    sim = sim or repay_sim(b)
    k = diffm(S, M)
    return p + (sim["sched"][k]["total"] if k < len(sim["sched"]) else 0)


def skip_impact(b, M, sim=None):
    """Cost of skipping debt payments in month M: delay in months and extra interest."""
    v, s = month_view(b, M), b["settings"]
    alloc = max(0, v["buffer"]) * s["debt_pct"] / 100
    if M >= plan_start(b):  # compare "skip M" with "pay M", whatever the flag says now
        base, alt = repay_sim(b, skip=M), repay_sim(b, unskip=M)
    else:  # already happened: compare with having paid the allocation
        base, alt = sim or repay_sim(b), repay_sim(b, extra_paid=alloc)
    delay = (base["months"] - alt["months"]) if None not in (base["months"], alt["months"]) else None
    return {"allocation": alloc, "delay_months": delay, "extra_interest": base["interest"] - alt["interest"]}


def brief(b, M):
    v = month_view(b, M)
    return {"month": M, "income": v["income"], "spending": v["spending"], "saved": v["ef"] + v["sip"],
            "debt_paid": paid(b, M, "owe"), "buffer": v["buffer"],
            "skipped": [k for k in ("ef", "sip", "debt") if v["skip_" + k]], "note": v["note"]}


def snapshot(b, M):
    sim, v = repay_sim(b), month_view(b, M)
    planned = planned_debt(b, M, sim)
    ex = [{"id": e["id"], "name": e["name"], "amount": e["amount"], "type": e["type"], "priority": e["priority"],
           "repeats_monthly": bool(e["recurring"])} for e in v["expenses"]]
    return {
        "today": today().isoformat(), "viewing_month": M,
        "month": {**{k: v[k] for k in ("salary", "extra_income", "income", "spending", "ef", "sip", "buffer", "left",
                                       "ef_target", "by_priority", "skip_ef", "skip_sip", "skip_debt", "note")},
                  "emergency_fund_saved": b["settings"]["ef_saved"],
                  "debt_payment_planned_or_made": planned, "debt_paid_so_far": paid(b, M, "owe"),
                  "SPENDABLE_AFTER_DEBT_PLAN": v["buffer"] - planned, "expenses": ex},
        "previous_month": brief(b, addm(M, -1)),
        "recent_months": [brief(b, addm(M, -i)) for i in range(5, -1, -1)],
        "debts": [d for d in b["debts"] if d["amount"] > 0],
        "debt_settings": {"share_of_buffer_for_debt_percent": b["settings"]["debt_pct"], "order": b["settings"]["debt_strategy"]},
        "repayment_plan": {k: sim[k] for k in ("months", "interest", "shortfall", "mins", "order", "owe_total", "lent_total")},
        "cost_of_skipping_debt_this_month": skip_impact(b, M, sim),
        "standing_rules": [{"id": n["id"], "rule": n["text"]} for n in b["notes"]],
        "forecast_1y": forecast(b, M, 12), "forecast_3y": forecast(b, M, 36)}


# ---------- API models ----------
YM = r"^\d{4}-\d{2}$"


class Settings(BaseModel):
    ef_saved: float = Field(0, ge=0)
    ef_months: float = Field(6, ge=0)
    ret: float = 12
    hike: float = 8
    infl: float = 5
    debt_pct: float = Field(100, ge=0, le=100)
    debt_strategy: Literal["avalanche", "snowball"] = "avalanche"


class MonthRow(BaseModel):
    month: str = Field(pattern=YM)
    salary: float | None = Field(None, ge=0)
    ef: float | None = Field(None, ge=0)
    sip: float | None = Field(None, ge=0)
    extra_income: float = Field(0, ge=0)
    skip_ef: bool = False
    skip_sip: bool = False
    skip_debt: bool = False
    note: str = ""


class Expense(BaseModel):
    id: int | None = None
    name: str = ""
    amount: float = Field(0, ge=0)
    type: Literal["fixed", "variable"] = "variable"
    priority: Literal["essential", "important", "optional"] = "optional"
    recurring: bool = True
    start: str = Field(pattern=YM)
    until: str | None = Field(None, pattern=YM)


class Debt(BaseModel):
    id: int | None = None
    person: str = ""
    direction: Literal["owe", "lent"] = "owe"
    amount: float = Field(0, ge=0)  # outstanding balance
    rate: float = Field(0, ge=0)  # % per year, only used for money I owe
    monthly: float = Field(0, ge=0)  # owe: minimum payment; lent: expected back per month
    due: str = ""
    note: str = ""


class Payment(BaseModel):
    id: int | None = None
    debt_id: int | None = None
    person: str = ""
    direction: Literal["owe", "lent"] = "owe"
    month: str = Field(pattern=YM)
    amount: float = Field(0, ge=0)


class Note(BaseModel):
    id: int | None = None
    text: str = ""


class Budget(BaseModel):
    settings: Settings
    months: list[MonthRow] = []
    expenses: list[Expense] = []
    debts: list[Debt] = []
    payments: list[Payment] = []
    notes: list[Note] = []


class Msg(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatIn(BaseModel):
    messages: list[Msg]
    month: str | None = Field(None, pattern=YM)


# ---------- chat tools (executed against SQLite) ----------
def _t(name, desc, props, req=()):
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": {
        "type": "object", "properties": props, "required": list(req)}}}


_N, _S, _B = {"type": "number"}, {"type": "string"}, {"type": "boolean"}
_TYPE = {"type": "string", "enum": ["fixed", "variable"]}
_PRI = {"type": "string", "enum": list(PRIORITIES), "description": "how much it matters to the user"}
_DIR = {"type": "string", "enum": ["owe", "lent"], "description": "owe = I owe them; lent = they owe me"}
_MON = {**_S, "description": "YYYY-MM; defaults to the month the user is viewing"}
_DEBT_PROPS = {"person": _S, "direction": _DIR, "amount": {**_N, "description": "outstanding balance in INR"},
               "rate": {**_N, "description": "% per year, for money I owe"},
               "monthly": {**_N, "description": "owe: minimum monthly payment; lent: expected back per month"},
               "due": {**_S, "description": "YYYY-MM-DD"}, "note": _S}
TOOLS = [
    _t("add_expense", "Add an expense. repeats_monthly=false makes it a one-time expense for that month only (trips, gifts, repairs).",
       {"name": _S, "amount": _N, "type": _TYPE, "priority": _PRI, "repeats_monthly": _B, "month": _MON}, ["name", "amount"]),
    _t("update_expense", "Change an expense from the given month onward (earlier months keep their old values).",
       {"id": _N, "name": _S, "amount": _N, "type": _TYPE, "priority": _PRI, "month": _MON}, ["id"]),
    _t("remove_expense", "Stop/delete an expense from the given month onward. Only when the user explicitly asks.",
       {"id": _N, "month": _MON}, ["id"]),
    _t("set_month_values", "Set values for a month. salary/ef/sip apply from that month onward; extra_income, skip_* and note apply to that month only. "
       "skip_debt=true pauses debt repayment, skip_ef / skip_sip pause those savings for that month.",
       {"month": _MON, "salary": _N, "ef": _N, "sip": _N, "extra_income": _N, "skip_ef": _B, "skip_sip": _B,
        "skip_debt": _B, "note": _S}),
    _t("set_settings", "Change global settings: emergency fund saved so far, target months, return/hike/inflation %, "
       "debt_pct (share of free buffer used for debts, 0-100) and debt_strategy.",
       {**{k: _N for k in SETTINGS if k != "debt_strategy"}, "debt_strategy": {"type": "string", "enum": ["avalanche", "snowball"]}}),
    _t("add_debt", "Record money I owe someone, or money I lent someone.", _DEBT_PROPS, ["person", "direction", "amount"]),
    _t("update_debt", "Change a debt/loan by id.", {"id": _N, **_DEBT_PROPS}, ["id"]),
    _t("remove_debt", "Delete a debt/loan. Only when the user explicitly asks.", {"id": _N}, ["id"]),
    _t("record_payment", "Record a payment made (owe) or received (lent); lowers the balance and is logged in that month.",
       {"id": _N, "amount": _N, "month": _MON}, ["id", "amount"]),
    _t("add_rule", "Save a standing rule or priority the user stated (e.g. 'all free buffer goes to papa until cleared'). "
       "Use whenever the user states a lasting intention.", {"text": _S}, ["text"]),
    _t("remove_rule", "Delete a saved rule by id.", {"id": _N}, ["id"]),
]
_DEBT_EDIT = [c for c in DEBT_COLS if c != "id"]


def _expense_edit(c, a, M):
    """Edit from month M onward; a recurring expense that started earlier is split so history is untouched."""
    row = c.execute("select * from expenses where id=?", (int(a["id"]),)).fetchone()
    if not row:
        return False
    if row["recurring"] and row["start"] < M:
        c.execute("insert into expenses(name,amount,type,priority,recurring,start,until) values(?,?,?,?,1,?,?)",
                  (row["name"], row["amount"], row["type"], row["priority"], row["start"], addm(M, -1)))
        c.execute("update expenses set start=? where id=?", (M, row["id"]))
    sets = {k: a[k] for k in ("name", "amount", "type", "priority") if k in a}
    if sets:
        c.execute("update expenses set " + ",".join(f"{k}=?" for k in sets) + " where id=?", [*sets.values(), row["id"]])
    return True


def run_tool(name, a, changes, M):
    M = a.get("month") or M
    if not re.match(YM, M):
        return "error: month must be YYYY-MM"
    with db() as c:
        if name == "add_expense":
            rec = a.get("repeats_monthly", True)
            c.execute("insert into expenses(name,amount,type,priority,recurring,start) values(?,?,?,?,?,?)",
                      (a["name"], a["amount"], a.get("type", "variable"), a.get("priority", "optional"), int(rec), M))
            changes.append(f"Added {a['name']} {inr(a['amount'])} ({'monthly from' if rec else 'one-time in'} {M})")
        elif name == "update_expense":
            if not _expense_edit(c, a, M):
                return f"error: no expense with id {a['id']}"
            changes.append(f"Updated expense #{int(a['id'])} from {M}: " + ", ".join(
                f"{k}={a[k]}" for k in ("name", "amount", "type", "priority") if k in a))
        elif name == "remove_expense":
            row = c.execute("select * from expenses where id=?", (int(a["id"]),)).fetchone()
            if not row:
                return f"error: no expense with id {a['id']}"
            if row["recurring"] and row["start"] < M:
                c.execute("update expenses set until=? where id=?", (addm(M, -1), row["id"]))
            else:
                c.execute("delete from expenses where id=?", (row["id"],))
            changes.append(f"Removed {row['name']} from {M}")
        elif name == "set_month_values":
            c.execute("insert into months(month) values(?) on conflict(month) do nothing", (M,))
            for k in ("salary", "ef", "sip", "extra_income", "skip_ef", "skip_sip", "skip_debt", "note"):
                if k in a:
                    c.execute(f"update months set {k}=? where month=?", (int(a[k]) if k.startswith("skip") else a[k], M))
                    changes.append(f"{M}: {k} = {a[k]}")
        elif name == "set_settings":
            for k, v in a.items():
                if k in SETTINGS:
                    c.execute(f"update settings set {k}=? where id=1", (v,))
                    changes.append(f"Set {k} to {v}")
        elif name == "add_debt":
            d = {"rate": 0, "monthly": 0, "due": "", "note": "", **a}
            c.execute("insert into debts(person,direction,amount,rate,monthly,due,note) values(?,?,?,?,?,?,?)",
                      [d[k] for k in _DEBT_EDIT])
            changes.append(("Owe " if d["direction"] == "owe" else "Lent to ") + f"{d['person']} {inr(d['amount'])} added")
        elif name == "update_debt":
            sets = {k: a[k] for k in _DEBT_EDIT if k in a}
            if not sets or not c.execute("update debts set " + ",".join(f"{k}=?" for k in sets) + " where id=?",
                                         [*sets.values(), int(a["id"])]).rowcount:
                return f"error: no debt with id {a['id']}"
            changes.append(f"Updated debt #{int(a['id'])}")
        elif name == "remove_debt":
            row = c.execute("select person from debts where id=?", (int(a["id"]),)).fetchone()
            if not row:
                return f"error: no debt with id {a['id']}"
            c.execute("delete from debts where id=?", (int(a["id"]),))
            changes.append(f"Removed debt with {row['person']}")
        elif name == "record_payment":
            row = c.execute("select * from debts where id=?", (int(a["id"]),)).fetchone()
            if not row:
                return f"error: no debt with id {a['id']}"
            amt = min(a["amount"], row["amount"])
            c.execute("update debts set amount=? where id=?", (row["amount"] - amt, row["id"]))
            c.execute("insert into payments(debt_id,person,direction,month,amount) values(?,?,?,?,?)",
                      (row["id"], row["person"], row["direction"], M, amt))
            owe = row["direction"] == "owe"
            changes.append(f"{'Paid' if owe else 'Received'} {inr(amt)} {'to' if owe else 'from'} {row['person']} ({M})")
        elif name == "add_rule":
            c.execute("insert into notes(text) values(?)", (a["text"],))
            changes.append(f"Saved rule: {a['text']}")
        elif name == "remove_rule":
            c.execute("delete from notes where id=?", (int(a["id"]),))
            changes.append("Removed a rule")
        else:
            return f"error: unknown tool {name}"
    return "ok"


def system_prompt(b, M):
    return f"""You are the assistant inside Budget Desk, a personal budget app for someone in India. All money is INR.
Below is the user's LIVE data for the month they are viewing ({M}), with every number already computed. Never recompute from raw rows when a computed field exists.
{json.dumps(snapshot(b, M), ensure_ascii=False, default=round)}

How to read it:
- buffer = income - spending - emergency fund - SIP. Part of the buffer is already committed to repaying debts (debt_payment_planned_or_made). Only SPENDABLE_AFTER_DEBT_PLAN is free for purchases. If it is 0 or negative, nothing is free, even if buffer is positive.
- Debts: "owe" = the user owes that person, "lent" = that person owes the user. skip_debt means the user paused repayment that month, which frees the money for that month but delays debt-free (see cost_of_skipping_debt_this_month).
- Expense priority: essential = must pay, important = matters to the user, optional = can be cut. Cut suggestions must come from optional first, then important only if the user agrees; never essential.
- repeats_monthly=false items are one-time for that month only. Changes to a recurring item apply from the viewed month onward; past months are not rewritten.
- standing_rules are the user's lasting instructions. Obey them in every answer.

Rules:
- Answer briefly, under 150 words. Plain text, "- " for bullets. Use ₹ with Indian grouping (₹1,25,000) and lakh/crore forms (₹2.58 L, ₹1.2 Cr).
- Never print field names or code identifiers (like SPENDABLE_AFTER_DEBT_PLAN) in a reply; say "free to spend after your debt plan".
- Do the arithmetic before suggesting options. An option only counts if it really makes the price fit: skipping debt adds debt_payment_planned_or_made to what is free this month; trimming optional items adds by_priority.optional per month. If even all options together cannot cover the price, say so plainly and give how many months of saving it takes (price divided by what is free each month, rounded up). State the cost of skipping debt as the delay in months and extra interest from cost_of_skipping_debt_this_month, not as the amount freed.
- For "can I buy ..." questions, start with Yes / Wait / No, then the numbers: fit against SPENDABLE_AFTER_DEBT_PLAN (not the raw buffer), EMI vs cash, effect on the emergency fund, SIP and debt plan. If it only fits by diverting debt money, say No/Wait and offer the options with their cost: skip debt this month (cost from cost_of_skipping_debt_this_month), trim optional items (name them), EMI, or wait N months.
- Protect the emergency fund and SIP unless the user says otherwise.
- When the user states a lasting intention (e.g. "all my buffer goes to debt", "never touch SIP", "this trip matters more than X"), apply it with the tools (e.g. set_settings debt_pct=100) AND save it with add_rule. For a one-month change (trip, bonus, skipped payment) use set_month_values / one-time expenses for that month, not standing changes.
- Use tools to change data when asked; remove things only when explicitly asked. After changing, confirm briefly with the new numbers you can see in the next snapshot.
- For big decisions, mention once that you are not a licensed financial advisor."""


# ---------- app ----------
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
if os.getenv("RENDER") and not APP_PASSWORD:  # Render sets RENDER=true; never run an open server online
    raise RuntimeError("APP_PASSWORD must be set on a deployed server")
init()
app = FastAPI(title="Budget Desk")
if not APP_PASSWORD:  # local mode: refuse other Host headers so a website cannot reach the API via DNS rebinding
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1"])


LOGIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Budget Desk</title>
<style>:root{color-scheme:light dark;--bg:#f3efe6;--card:#fffdf9;--ink:#1c1a2e;--muted:#6b6780;--line:#e7e0d1;--accent:#4f46e5;--bad:#b9302a}
@media(prefers-color-scheme:dark){:root{--bg:#0e0f1f;--card:#171832;--ink:#ecebf7;--muted:#9b99b6;--line:#2b2d50;--accent:#8b85ff;--bad:#ff7a70}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,sans-serif}
form{width:100%;max-width:340px;background:var(--card);border:1px solid var(--line);border-radius:18px;padding:28px;box-shadow:0 12px 28px -16px rgba(40,30,10,.3)}
.logo{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,var(--accent),#7c74ff);color:#fff;display:grid;place-items:center;font:600 22px system-ui}
h1{font-size:1.3rem;margin:14px 0 2px}p{margin:0 0 18px;color:var(--muted);font-size:.88rem}
input{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;background:transparent;color:inherit;font:inherit}
input:focus{outline:2px solid var(--accent);outline-offset:1px}
button{width:100%;margin-top:12px;padding:11px;border:0;border-radius:10px;background:var(--accent);color:#fff;font:600 1rem system-ui;cursor:pointer}
.err{color:var(--bad);font-size:.85rem;margin:8px 0 0}</style></head><body>
<form method="post" action="/login"><div class="logo">&#8377;</div><h1>Budget Desk</h1><p>Enter your password to continue.</p>
<input type="password" name="password" placeholder="Password" autocomplete="current-password" autofocus required>
{error}<button>Unlock</button></form></body></html>"""


def _session_token():
    return hmac.new(APP_PASSWORD.encode(), b"budget-desk-session", hashlib.sha256).hexdigest()


def _authed(request: Request):
    """Valid session cookie (browser) or HTTP Basic password (curl/scripts)."""
    cookie = request.cookies.get("bd_session", "")
    if cookie and secrets.compare_digest(cookie, _session_token()):
        return True
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            pw = base64.b64decode(auth[6:]).decode().partition(":")[2]
            return secrets.compare_digest(pw.encode(), APP_PASSWORD.encode())
        except ValueError:
            pass
    return False


@app.middleware("http")
async def require_password(request: Request, call_next):
    """Off when APP_PASSWORD is unset (local use). Otherwise pages redirect to /login; the API answers 401."""
    if not APP_PASSWORD or request.url.path in ("/login", "/logout") or _authed(request):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return Response("Password required", 401)
    return RedirectResponse("/login", 303)


@app.get("/login")
def login_page():
    return HTMLResponse(LOGIN_HTML.replace("{error}", "")) if APP_PASSWORD else RedirectResponse("/", 303)


@app.post("/login")
async def login(request: Request):
    if not APP_PASSWORD:
        return RedirectResponse("/", 303)
    pw = parse_qs((await request.body()).decode()).get("password", [""])[0]
    if secrets.compare_digest(pw.encode(), APP_PASSWORD.encode()):
        r = RedirectResponse("/", 303)
        r.set_cookie("bd_session", _session_token(), max_age=60 * 60 * 24 * 30, httponly=True, samesite="lax",
                     secure=request.headers.get("x-forwarded-proto") == "https")
        return r
    await asyncio.sleep(0.5)  # slows password guessing
    return HTMLResponse(LOGIN_HTML.replace("{error}", '<p class="err">Wrong password</p>'), 401)


@app.get("/logout")
def logout():
    r = RedirectResponse("/login", 303)
    r.delete_cookie("bd_session")
    return r


@app.get("/")
def index():
    return FileResponse(HERE / "static" / "index.html")


@app.get("/api/budget")
def get_budget():
    return load()


@app.put("/api/budget")
def put_budget(b: Budget):
    try:
        save(b.model_dump())
    except sqlite3.IntegrityError as e:
        raise HTTPException(400, f"Duplicate id: {e}")
    except RuntimeError as e:  # Turso reports constraint errors as RuntimeError
        raise HTTPException(400, str(e))
    return load()


@app.post("/api/chat")
def chat(body: ChatIn):
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(503, "OPENAI_API_KEY is not set. Add it to .env and restart the server.")
    hist = [m.model_dump() for m in body.messages[-16:]]
    while hist and hist[0]["role"] != "user":
        hist.pop(0)
    if not hist:
        raise HTTPException(400, "No user message.")
    M = body.month or cur()
    msgs = [{"role": "system", "content": ""}, *hist]
    changes, client, reply = [], openai.OpenAI(), ""
    try:
        for _ in range(8):  # tool-call loop; the system prompt is rebuilt so the model sees fresh state
            msgs[0]["content"] = system_prompt(load(), M)
            m = client.chat.completions.create(model=MODEL, messages=msgs, tools=TOOLS).choices[0].message
            msgs.append(m.model_dump(exclude_none=True))
            reply = m.content or ""
            if not m.tool_calls:
                break
            for tc in m.tool_calls:
                try:
                    out = run_tool(tc.function.name, json.loads(tc.function.arguments), changes, M)
                except (KeyError, TypeError, ValueError, sqlite3.Error) as e:
                    out = f"error: {e}"
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": out})
    except openai.OpenAIError as e:
        raise HTTPException(502, f"OpenAI API error: {e}")
    return {"reply": reply or "Done.", "changes": changes, "budget": load()}
