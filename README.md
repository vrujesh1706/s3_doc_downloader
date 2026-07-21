# S3 Document Downloader

Small FastAPI project for searching account, encounter, client, and facility filters, then downloading selected S3 document outputs as a ZIP.

## Setup

```bash
cd /home/vrujeshnavdiya/Python-Work/s3_doc_downloader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` with DB and AWS credentials. Do not put real credentials in Git or notebooks.

The app talks to two separate database/S3 environments, each with its own credential set: `PROD_*` and `STAGING_*` (e.g. `PROD_DB_HOST`, `STAGING_S3_BUCKET`). Both must be filled in — the UI has an Environment dropdown (Production/Staging) next to the client picker that switches which one a search/download uses.

## Run

```bash
source .venv/bin/activate
set -a
source .env
set +a
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open:

```text
http://localhost:8000
```

## Filters

The UI accepts comma, space, or newline separated values for:

- Account numbers
- Encounter IDs

Select one or more output file types, search, then download all returned matching files as a ZIP.
