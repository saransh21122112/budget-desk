# Budget Desk

Local personal budget app (INR, Indian digit grouping). FastAPI + SQLite backend, one static `static/index.html`, no build step.

## Setup
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your OPENAI_API_KEY in .env
```

## Run
```bash
uvicorn main:app --reload
```
Open http://localhost:8000. Data lives in `budget.db` (created and seeded on first run). An older single-month database is migrated automatically and backed up next to it as `budget.db.bak-<timestamp>`.

## How months work
- Use the month and year pickers in the header. Every section shows that month.
- **Monthly expenses** repeat from the month they start. Editing one changes it from the viewed month onward; earlier months keep their old values. Deleting one stops it from that month.
- **One-time expenses** ("This month only") count only in their month and never carry forward.
- **Salary, emergency fund, SIP** carry forward from the month you last set them.
- **Only for this month:** extra income, a note, and "skip debt payment / emergency fund / SIP". These never carry forward. The month report and the next month's report show what the pause cost.
- **Priority tags** (Essential / Important / Optional) drive the report and the assistant's cut suggestions.

## Debts
Track money you owe and money you lent. Repayment uses each month's own free buffer (share set by you), plus money expected back, so one-time costs, extra income and skipped months change the plan. Pay order is highest interest first or smallest balance first. Payments are logged in the month you record them.

## Assistant
OpenAI chat with tool calling (`OPENAI_MODEL`, default `gpt-4o-mini`). It is given the viewed month's computed numbers (including what is free after the debt plan), your debts, recent months and your standing rules, and it can edit all of them. State a lasting intention ("all my buffer goes to papa") and it saves it as a rule. The key stays on the server.

## Deploy (Render)
`render.yaml` + `Dockerfile` are ready. Push this folder to a **private** GitHub repo, then in Render choose New > Blueprint, pick the repo, and type `APP_PASSWORD` and `OPENAI_API_KEY` when asked. The Starter plan with a 1 GB disk (about $7.25/month) is needed so the SQLite file survives restarts. The server refuses to start online without `APP_PASSWORD`; every page and API call then asks for it (any username, that password).

The cloud copy starts with seed data. To bring your data across, use Render's shell (`Shell` tab) to upload or recreate it, and keep using one copy only (local and cloud do not sync). `APP_TZ` (default Asia/Kolkata) decides which month "today" is.

## Test
`python test_budget.py` uses throwaway databases: derived numbers, month semantics, history-preserving edits, repayment plan, skip cost, migration.
