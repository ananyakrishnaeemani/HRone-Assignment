# Employee Attendance & Analytics API

FastAPI + PyMongo service for the HROne engineering assignment.

## Start

```bash
python -m venv .venv && source .venv/bin/activate   # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
cp .env.example .env    # set MONGO_URI and MONGO_DB
uvicorn app.main:app --port 8000
```

MongoDB 6.0 or newer required. All indexes are created automatically at startup.

## Environment variables

| Variable   | Description                          |
|------------|--------------------------------------|
| `MONGO_URI` | MongoDB connection string            |
| `MONGO_DB`  | Database name                        |

Real environment variables always take priority over `.env`.

## What is implemented

All endpoints from `openapi.yaml` are implemented in `app/main.py`:

- `GET /health`
- `POST /employees`, `GET /employees`
- `POST /attendance/punch-in`, `POST /attendance/punch-out`
- `GET /attendance`, `PATCH /attendance/{emp_code}/{date}`
- `GET /analytics/employees/{emp_code}/monthly`
- `GET /analytics/departments/summary`
- `GET /analytics/leaderboard/late`
- `GET /analytics/departments/{department}/trend`
- `GET /admin/explain/{endpoint}`

