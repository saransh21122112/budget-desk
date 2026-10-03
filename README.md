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

## Deploy free (Render + Turso)
Render's free plan has no persistent disk, so the data lives in a free [Turso](https://turso.tech) database instead of `budget.db`.
1. Create a Turso database (any name) and a database token. Note its `libsql://...` URL.
2. Push this folder to a **private** GitHub repo. In Render: New > Blueprint > pick the repo (`render.yaml` is read).
3. Type `APP_PASSWORD` (long), `OPENAI_API_KEY`, `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` when asked.
4. Open the Render URL; the browser asks for the password (any username).
The server refuses to start online without `APP_PASSWORD`. The free service sleeps after ~15 minutes idle (first request ~1 minute); your data is safe in Turso. The cloud starts with sample data; copy yours across once with
`curl -s localhost:8000/api/budget > b.json` then `curl -u me:PASSWORD -X PUT https://YOUR-APP.onrender.com/api/budget -H 'content-type: application/json' --data @b.json`.
Afterwards use one copy only (local and cloud do not sync). `APP_TZ` (default Asia/Kolkata) decides which month "today" is.

## Test
`python test_budget.py` uses throwaway databases: derived numbers, month semantics, history-preserving edits, repayment plan, skip cost, migration, and the Turso client (rollback on a failed save).
