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

Select one or more output file types, search, then download the matching files as a ZIP.

## File outputs

Each result can carry several S3 document outputs. Available types:

- PCS XML result, E/M request/result, CPT request/result, CMCS result (encounter-level)
- Original document and PDF document (`document_mst.path` and `document_mst.orignal_path`, per document)

If no type is selected, all types are included.

## Results

Results are grouped **one row per encounter**. Because a single encounter can have
multiple documents, each row shows:

- **Multi-doc** — `Yes · N docs` when the encounter has more than one document, else `No`.
- **Files to download** — a per-type breakdown of the distinct files that will land in
  the ZIP for that encounter (e.g. `5 PCS XML result, 1 CPT request`).

The table is paginated (rows-per-page selector + Prev/Next). Row selection is preserved
across pages, so a download includes everything you have checked on any page.

## Download

Downloads run as a background job so the UI can show live progress:

- A **progress bar** reports `x / N encounters` as the ZIP is built.
- S3 objects are fetched **in parallel** (`DOWNLOAD_CONCURRENCY` in
  `app/download_service.py`, default 8); the ZIP itself is written single-threaded.
- **De-duplication:** encounter-level files (the shared request/result JSONs) are
  written once per encounter folder, while unique per-document files (PCS XML, the
  original/PDF documents) are all kept. An object that can't be fetched is recorded as a
  `FAILED_*.txt` note in the ZIP and counted under "skipped".

Folder structure is selectable: **Facility wise** (`facility/encounter_account/…`) or
**One folder** (`encounter_account/…`).

## Safety limits

There is no user-facing result cap. Two optional env values bound the work as a safety
net (see `.env.example`):

- `MAX_SEARCH_ROWS` (default 5000) — max rows a search returns.
- `MAX_DOWNLOAD_FILES` (default 2500) — max files written into a single ZIP.

## Notes

- Progress is tracked in-memory in a single server process; run the app with one worker
  (the default `uvicorn` command above), not multiple workers.
- Static assets are cache-busted with a `?v=N` query string in `app/static/index.html`;
  bump it when editing `app.js`/`styles.css`.
