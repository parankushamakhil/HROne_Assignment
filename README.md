# Employee Attendance API

A FastAPI and MongoDB API for employee attendance, corrections, and analytics. The implementation focuses on reliable
IST and overnight-shift calculations, safe concurrent writes, and aggregation queries that use MongoDB indexes.

## Run locally

Requires Python 3.11+ and MongoDB 6.0+.

1. Create and activate a virtual environment, then install dependencies:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and set `MONGO_URI` to your local MongoDB or Atlas connection string. Keep `.env` private.
3. Start the API:

   ```powershell
   uvicorn app.main:app --port 8000
   ```

4. Open [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) for interactive API documentation.

## Checks

Run the unit tests:

```powershell
python -m unittest discover -s tests -v
```

Run the HTTP, MongoDB aggregation, and concurrency checks:

```powershell
python tests/run_mongo_integration.py
```

The integration runner creates a uniquely named temporary database, seeds edge cases, and drops that database when the
run finishes. It uses `MONGO_TEST_URI` when set, otherwise `MONGO_URI` from the environment or `.env`. The MongoDB user
needs permission to create and drop databases. The runner does not write to the configured application database.

Indexes are created automatically at startup. See [DECISIONS.md](DECISIONS.md) for key implementation choices and
[REVIEW.md](REVIEW.md) for the starter-code findings addressed.
