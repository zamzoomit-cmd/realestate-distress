# Real Estate Distress Lead Scraping & Scoring System
# Arizona Counties: Maricopa + Pima

## Quick Start

### 1. Local dev with Docker
```bash
cp .env.example .env
# Edit .env — add GHL_WEBHOOK_URL if you have it

docker compose up -d db        # start Postgres (auto-runs schema SQL)
docker compose up -d api       # start FastAPI on :8000
```

### 2. Run a scraper manually
```bash
# Run a single source
docker compose run --rm scraper_daily python -m jobs.runner --job scraper --source-key maricopa_recorder_foreclosure

# Run all sources
docker compose run --rm scraper_daily python -m jobs.runner --job daily_scrape_all

# Re-score all properties
docker compose run --rm scraper_score python -m jobs.runner --job daily_score_all

# Push hot leads to GHL
docker compose run --rm scraper_daily python -m jobs.runner --job push_hot_leads
```

### 3. API endpoints
```
GET  http://localhost:8000/api/leads           # paginated lead list + filters
GET  http://localhost:8000/api/leads/{id}      # single lead detail
GET  http://localhost:8000/api/leads/export/csv
POST http://localhost:8000/api/leads/{id}/push-ghl
POST http://localhost:8000/api/ghl/push-hot    # push ALL hot leads
GET  http://localhost:8000/api/stats           # dashboard summary
GET  http://localhost:8000/api/sources         # source registry
GET  http://localhost:8000/api/scrape-runs     # run history
POST http://localhost:8000/api/scrape/trigger?source_key=maricopa_recorder_foreclosure
POST http://localhost:8000/api/score/all
GET  http://localhost:8000/health
```

### 4. Deploy to Render
```bash
# Push to GitHub, then connect repo in Render dashboard
# Or use the render.yaml blueprint:
render blueprint apply render.yaml
```

Set these env vars in Render:
- `DATABASE_URL` — auto-set from the linked Supabase/Render Postgres
- `GHL_WEBHOOK_URL` — your GoHighLevel webhook URL
- `GHL_API_KEY` — optional GHL API key

### 5. Use Supabase Postgres
```bash
# Get connection string from Supabase dashboard > Settings > Database
# Format: postgresql://postgres:[password]@[host]:5432/postgres

# Run schema on Supabase
psql $DATABASE_URL -f sql/001_schema.sql
psql $DATABASE_URL -f sql/002_seed_sources.sql
```

---

## Architecture

```
realestate-distress/
├── sql/
│   ├── 001_schema.sql        # All tables, enums, indexes, views
│   └── 002_seed_sources.sql  # Source registry for AZ counties
├── core/
│   ├── config.py             # Settings, DB pool, upsert helpers
│   ├── base_scraper.py       # Base class: rate limiting, raw storage, logging
│   ├── address_utils.py      # APN normalization, address parsing, owner flags
│   ├── enrichment.py         # Property find-or-create, APN matching, equity
│   └── scoring.py            # Signal detection, lead scoring, tier assignment
├── scrapers/
│   ├── maricopa/scrapers.py  # Recorder, Assessor CSV, Treasurer, Code violations
│   └── pima/scrapers.py      # Recorder, Assessor GIS, Treasurer, Probate
├── api/main.py               # FastAPI: leads, export, GHL push, stats, triggers
├── jobs/runner.py            # Job runner: daily scrape, score, link, GHL push
├── Dockerfile
├── docker-compose.yml
├── render.yaml               # Render deploy blueprint with cron jobs
└── requirements.txt
```

## Lead Scoring

| Signal                   | Points |
|--------------------------|--------|
| Foreclosure notice       | 100    |
| Auction within 45 days   | 80     |
| High equity ($150k+)     | 75     |
| Probate                  | 70     |
| Tax delinquency          | 60     |
| Open code violation      | 55     |
| Out of state owner       | 45     |
| Absentee owner           | 35     |
| Stacked signals bonus    | +50    |

| Tier  | Score     |
|-------|-----------|
| Hot   | 250+      |
| Warm  | 150–249   |
| Watch | 75–149    |
| Ignore| < 75      |

## Data Sources

| County    | Source            | Method      |
|-----------|-------------------|-------------|
| Maricopa  | Recorder (FC/NOD) | Playwright  |
| Maricopa  | Assessor CSV      | requests    |
| Maricopa  | Treasurer (tax)   | Playwright  |
| Maricopa  | Code compliance   | requests    |
| Pima      | Recorder (FC/NOD) | Playwright  |
| Pima      | Assessor GIS/REST | requests    |
| Pima      | Treasurer (tax)   | requests    |
| Pima      | Superior Ct (prob)| Playwright  |

## Scheduled Jobs (Render Cron)

| Job                   | Schedule         |
|-----------------------|------------------|
| Hourly foreclosure    | Every hour       |
| Daily full scrape     | 2 AM MST daily   |
| Daily scoring         | 4 AM MST daily   |
| Orphan linker         | 4:30 AM MST daily|
| GHL hot push          | Monday 6 AM MST  |
