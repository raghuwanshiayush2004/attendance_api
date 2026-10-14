# Employee Attendance & Analytics API

FastAPI + MongoDB (PyMongo). All code is in `app/main.py`.

## Run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# set MONGO_URI and MONGO_DB (env vars or a local .env file), e.g. a local mongo:7 container
uvicorn app.main:app --port 8000
```
Indexes are created automatically at startup. MongoDB 6.0+ is needed (`$setWindowFields`, `$densify`).

## Notes
- Everything in the contract is implemented, including the explain endpoint.
- `REVIEW.md` lists the defects I found in the starter code, `DECISIONS.md` has my design answers.
- Analytics queries use an index hint so the explain output always shows `IXSCAN`.
