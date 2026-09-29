# S3 Document Downloader — Development Guide

For setup and everyday use, see the [user guide](USERGUIDE_README.md). This file covers implementation and maintenance.

## 1. Stack and files

Python 3.10+; FastAPI/Uvicorn; SQLAlchemy/PyMySQL; boto3; plain HTML, CSS, and JavaScript. There is no frontend build step. The client query requires a database with window-function support, such as MySQL 8+.

| File | Responsibility |
| --- | --- |
| `app/main.py` | HTTP routes, validation, metadata routing, download jobs, and cleanup. |
| `app/config.py` | Load `.env`, validate settings, and build database URLs. |
| `app/database.py` | Cached engines and client database discovery. |
| `app/models.py` | Request models, file types, environments, and date presets. |
| `app/query_service.py` | Client SQL, file labels, result IDs, and document grouping. |
| `app/metadata_service.py` | Metadata filters, date windows, dropdowns, and client aliases. |
| `app/download_service.py` | S3 paths, filenames, deduplication, limits, and ZIP creation. |
| `app/static/index.html` | Page layout and controls. |
| `app/static/app.js` | Search, selection, pagination, progress, and theme behavior. |
| `app/static/styles.css` | Theme tokens, components, and responsive layout. |
| `tests/test_safety.py` | Offline regression checks with mocked database/S3 access. |
| `.env.example` / `requirements.txt` | Configuration template / pinned dependencies. |

## 2. Data flow

**Direct:** browser → selected environment's `CAPC_APIGATEWAY_<client>` database → document paths → selected environment's S3 bucket → ZIP.

**Metadata:** browser → shared `overall_data` table → encounter IDs grouped by client → direct query in each client database → S3 → ZIP. Metadata contains no S3 paths. Production/Staging changes the client database and bucket; commonDb is shared.

Client discovery uses `SHOW DATABASES`, excluding `SYSTEM` and `TEST`. The client query joins account, encounter, document, and processing tables; requires active accounts/encounters; and selects the latest processing records. It orders document rows by account rank, then service date, before applying the row limit.

Direct requests require exactly one nonempty account/encounter list. Values are trimmed and deduplicated. Database values use bound parameters. Results group by an ID derived from account number, encounter ID, client ID, facility ID, and service date; this is not a global encounter-ID key.

### Metadata behavior

- `MONTH_WINDOW = 3` limits the indexed `month` field to the current month and previous two months. Direct lookup has no explicit date-window filter.
- Date filters use `last_coding_date`. Custom dates must be a pair; the upper SQL bound is midnight after the final day. Presets use the server's local date.
- Different filter fields use AND; multiple values within a list use IN. Document types match either `enc_doc_code` or `enc_doc_type`.
- Metadata results have no `ORDER BY`. `truncated` is true when the metadata row count reaches the cap; it does not detect truncation in subsequent client queries.
- Client dropdown values come from recent `overall_data` rows. Facilities come from the client's lowercase rollup table, filtered on `coding_date`; missing rollup tables fall back to `client_facility_info`.
- Rollup table names must exist in the `SHOW TABLES` cache before interpolation. Dropdown/table caches last for the process lifetime; restart to refresh them.
- commonDb is contacted lazily. Connection failures return 503, so direct lookup can still work. Empty metadata searches may report the latest coding date for the non-date filters.

Client aliases in `CLIENT_CODE_ALIASES` are merged before querying webdb:

| Metadata code | Webdb client |
| --- | --- |
| `CHS_ED`, `CHS_HOSPITAL` | `CHS` |
| `PH_ANESTHESIA` | `PH` |

Missing client databases appear in `clients_skipped`. Add new service-line mappings in `app/metadata_service.py`.

## 3. Configuration and running

Use [`.env.example`](.env.example) as the full setting reference. `.env` loads automatically; existing process environment values take precedence. Both `PROD_*` and `STAGING_*` require database user, password, host, and bucket. `COMMON_DB_USER`, `COMMON_DB_HOST`, and `COMMON_DB_NAME` are required; set `COMMON_DB_PASSWORD` as needed by the server.

Client database ports default to 3306 and TLS defaults to enabled. The commonDb engine does not apply the client TLS setting. Credentials use SQLAlchemy `URL.create`, so special characters are handled without manual URL escaping.

S3 uses boto3's credential chain. For an AWS profile or role, remove placeholder access keys from `.env`; temporary credentials also need `AWS_SESSION_TOKEN`. The identity needs read access to the selected bucket's objects. Database access needs the SELECT and discovery operations used above.

| Setting | Default | Effect |
| --- | --- | --- |
| `MAX_SEARCH_ROWS` | `5000` | Cap on the metadata query and each client query, before result grouping. |
| `MAX_DOWNLOAD_FILES` | `2500` | Maximum planned files attempted; remaining files count as skipped. |
| `MAX_DOWNLOAD_BYTES` | `1073741824` | Aggregate S3 data budget per job (1 GiB); exceeding it fails the job. |
| `MAX_DOWNLOAD_JOBS` | `2` | Outstanding jobs, including completed ZIPs awaiting retrieval. |
| `DOWNLOAD_JOB_TTL_SECONDS` | `3600` | Completed/failed job retention; expiry sweeps run every minute. |

All numeric settings must be positive. Run from the project root because static paths are relative. After activating the virtual environment:

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Use reload only for development: restarting loses jobs. Run **one worker** because job state is in memory. For shared operation, omit reload and follow the user guide's shared-access instructions. The app has no built-in authentication.

Temporary storage uses the system temp directory (`TMPDIR` can override it). Allow roughly twice the byte limit per outstanding job, plus ZIP overhead. Server files are streamed; the browser still collects the final ZIP as a Blob before saving it.

## 4. API

Open [Swagger UI](http://localhost:8000/docs) on a running local server for request schemas and interactive calls. Schema JSON is at `/openapi.json`.

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/api/clients?environment=production` | Clients in the chosen environment. |
| GET | `/api/file-types` | Supported output keys and labels. |
| GET | `/api/metadata/clients` | Recent metadata client codes. |
| GET | `/api/metadata/facilities?client_code=CODE` | Facility values and labels. |
| POST | `/api/search` | Direct search. |
| POST | `/api/metadata/search` | Metadata search. |
| POST | `/api/download/start` | Start a direct download. |
| POST | `/api/metadata/download/start` | Start a metadata download. |
| GET | `/api/download/progress/{job_id}` | Job status, counts, and errors. |
| GET | `/api/download/file/{job_id}` | Retrieve the completed ZIP. |

Direct requests contain `environment`, `client`, one of `account_numbers` / `encounter_ids`, and nonempty `selected_files`. Metadata requests contain `environment`, `filters`, and nonempty `selected_files`.

Metadata API filters also accept account numbers, encounter IDs, document types, and multiple client/facility codes. The browser exposes single client/facility choices and dates. Empty API lists mean unrestricted; at least one real filter is required. The browser's explicit client/facility selection requirement is a UI rule.

Search responses include `count`, `document_count`, and grouped `rows`. Metadata adds match counts, matched/skipped clients, queried webdb clients, `truncated`, and sometimes `latest_coding_date`.

Downloads add `result_ids` (search row IDs) and `folder_structure` (`single_folder` or `facility_wise`). An empty API `result_ids` list downloads all matching rows; the browser requires a selection. Download start re-runs the search, so results are not a saved snapshot. Poll the returned job ID until `done` or `error`, then fetch the ZIP. Successful full delivery removes the job; interrupted transfers remain retryable until expiry. Polling an errored job removes it.

## 5. File handling

- `filePath` means Original document (`document_mst.path`); `orignal_path` means PDF document. Keep the existing spelling in API/schema fields.
- Document paths receive `ezcapc/` if missing. Stored S3 URI/bucket prefixes are stripped; retrieval always uses the configured environment bucket.
- Original filenames use account, encounter, facility code, worktype, and document ID, ending in `.txt`. Bytes are unchanged. Unsafe name characters become underscores; missing fields use `unknown_<field>`.
- Facility folders use the third segment of the stored original path, falling back to facility ID. See the user guide for ZIP layouts.
- Deduplication uses encounter folder plus normalized S3 key. Original-only flat downloads preserve copies belonging to different encounter folders. Filename collisions get numeric suffixes.
- Eight S3 fetches run in parallel (`DOWNLOAD_CONCURRENCY`), streaming in 1 MiB chunks. One thread writes the ZIP; only a batch of objects is staged at a time.
- Fetch failures produce `FAILED_*.txt` notes and logs. Files skipped by the count cap have no failure notes. If no files succeed, the job fails and its archive is removed.
- Temporary files are cleaned after failure, expiry, or full delivery. Active transfers are protected from expiry; partial/range requests do not consume the archive.

## 6. UI and design

The page uses a neutral light/dark palette, with red for errors. Colors live in CSS variables; update both dark-theme blocks when changing the palette. The theme follows system preference until the user chooses one, then saves that choice in `localStorage`.

Client/facility comboboxes match code prefixes and label-word prefixes. Hidden selects store values, with a `MutationObserver` syncing options and disabled state. Keep keyboard selection and focus behavior when editing these controls.

Selections live in a JavaScript `Set` across pages; new results start fully selected. Progress polls every 250 ms. Responsive styles and reduced-motion rules live in `styles.css`. Bump the `?v=` values in `index.html` after changing JS/CSS so browsers refresh cached assets.

## 7. Validation

From the project root, with the virtual environment active:

```bash
python -m unittest discover -s tests -v
```

Tests block network connections and mock databases/S3. They cover direct-filter validation, environment routing, streaming, names, deduplication, download limits, expiry, and transfer cleanup. They do not validate live credentials, database schemas, bucket contents, or browser interactions.

For a UI change, manually check both search tabs, dropdown keyboard use, paging/selection, ZIP layouts, and light/dark themes with team-approved test data. Keep usage changes in the user guide and implementation changes here.
