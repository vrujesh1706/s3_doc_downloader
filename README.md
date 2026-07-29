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

## Search rules

The short version of how filters combine. Each rule is explained in full further
down; the UI carries only a one-line hint per tab, so this is the reference.

| # | Rule | Why it matters |
| - | ---- | -------------- |
| 1 | **Every filter is `AND`.** Filling more boxes always narrows, never widens. | On direct lookup, an account list plus an unrelated encounter list matches **nothing** — people expect a union and get zero rows. |
| 2 | **From/To beat the Service date range, and must be a pair.** | Picking "Last 7 days" *and* a From date silently discards the preset. One-sided ranges are rejected outright, because they widened the search instead of narrowing it. |
| 3 | **Client and Facility are required on the metadata tab.** Blank is a form error. | "Search everything" has to be chosen with the explicit **All clients** / **All facilities** entry, not by leaving a box alone. |
| 4 | **Only the last three months are searchable.** | webdb keeps three months of documents, so an older encounter has no files to fetch. `overall_data` is bounded on its indexed `month` column, which is also what keeps these queries fast. |
| 5 | **A truncated result is a sample, not an answer.** | The row cap is applied with no `ORDER BY`, so a wide search returns an arbitrary slice that can cover only a few clients while looking complete. Check the `truncated` warning. |
| 6 | **At least one filter, and at least one file output.** | Both used to fall back to "everything", which pulled every file for every encounter. |
| 7 | **Direct lookup never touches commonDb.** | It stays usable while the commonDb server is down. |

## Search modes

The UI has two tabs. **Direct client lookup is the default**, because it only
needs the client webdb. Nothing in the app contacts commonDb until you click the
**Find by metadata** tab — the app starts, the page loads, and direct lookups and
downloads all work while the commonDb server is down.

### Find by metadata (commonDb)

Searches `overall_data` in commonDb — one table covering **every**
client — and then reads the file paths out of the per-client webdb. This is the
way to search without already knowing an account or encounter ID.

Filters. **Client code** and **Facility code** are required; the rest are
optional, and at least one filter overall must be set:

- **Service date range** — a preset (today, yesterday, last 7/30 days, this/last week,
  this/last month) or an explicit **From**/**To**. Explicit dates override the preset.
  `last_week` means the previous Monday–Sunday, not a rolling seven days.

  The override is total, so **From** and **To** must be supplied as a pair. One
  alone is rejected with a 400 rather than silently dropping the preset and
  leaving that side open-ended, which used to turn "Last 7 days" plus a From date
  into a far *wider* search than the preset alone:

  | Service date range | From    | To           | Actual range searched      |
  | ------------- | ------------ | ------------ | -------------------------- |
  | Last 7 days   | –            | –            | `2026-07-23 … 2026-07-29`  |
  | Last 7 days   | `2026-01-01` | `2026-03-31` | `2026-01-01 … 2026-03-31`  |
  | Last 7 days   | `2026-01-01` | –            | **rejected** — To missing   |
  | Last 7 days   | –            | `2026-03-31` | **rejected** — From missing |

  Every search is additionally bounded to the three-month retention window.
- **Client code** — a dropdown of the client codes present in `overall_data`.
  Required: pick a client, or the explicit **All clients** entry.
- **Facility code** — a dropdown that depends on the selected client, so it only
  ever lists facilities belonging to that client. Disabled until a client is picked.
  Each entry is labelled `code — description`, because the codes on their own are
  unreadable: most are bare tablespace numbers, and genuine facilities such as
  `2310_Clinic` or `WHS_CAPC` look like stray data until the name is next to them.

These two, plus the **Client** dropdown on the direct lookup tab, are
type-to-filter comboboxes rather than plain `<select>`s. The lists run long (SCP
has 252 facilities) and a native select's type-ahead resets after about a second
and cannot narrow over several keystrokes. Typing `s`
then `j` leaves only codes beginning `sj`. Matching is prefix-only — on the code
first, then on any word of the description, so `totowa` finds `ACP_TOTOWA` and
`PULM_TOTOWA`. It is deliberately not a substring match: that would make
"Table**s**pace" answer to `s`. Arrow keys move, Enter takes the highlighted row
(or the only remaining one), Escape restores the current selection.

Each `<select>` is still in the DOM, hidden, holding the value — `.value`,
`.disabled` and `change` listeners work as before, and a `MutationObserver` picks
up option refills, so the loading code needed no changes.

The dropdowns are served by `GET /api/metadata/clients` (a list of codes) and
`GET /api/metadata/facilities?client_code=…` (a list of `{value, label}`, where
`value` is the bare facility code the search expects), and they read from two
different tables. Clients come from `overall_data`, where `client_code` sits in the
`uk_overall_month_client_enc` index next to `month` so the query is answered from
the index alone. Facilities come from commonDb's per-client rollup tables — one
per client code, lowercased (`chs`, `chs_ed`, `scp`, …), each a monthly row per
facility — filtered to the retention window on `coding_date`. Asking
`overall_data` for both at once is what used to make this slow: `facility_code`
is not in that index, so including it forced a multi-million-row table scan
(~35s, against ~3s for clients and ~0.1s for facilities now). Both results are
cached in memory for the life of the process — restart the app to pick up a
newly added client or facility.

The rollup tables agree with `overall_data` almost exactly: across all 17 clients
they miss no facility and offer only 9 with no encounters in the window. A client
with no rollup table yet falls back to `client_facility_info`, a ~1.5k-row
reference table of every (client, facility) pair. That table is not the primary
source because it records mappings rather than activity — it lists 590 facilities
with no recent encounters, and maps some to the wrong client (`4695_ED` appears
under both `CHS` and `CHS_ED`, though in `overall_data` every `_ED` facility
belongs solely to `CHS_ED`).

Because a rollup table's name comes from the client code, it is interpolated into
the SQL rather than bound as a parameter. It is checked against a `SHOW TABLES`
whitelist first, so an unknown or hostile `client_code` can never reach the query.

They are loaded lazily: the first time you open this tab the client list is
built, which takes a few seconds and is the first thing to touch commonDb. If
commonDb is unreachable the endpoints return `503` with a message pointing at
direct lookup, nothing is cached, and clicking the tab again retries.

The API accepts more filters than the UI exposes (`account_numbers`,
`encounter_ids`, `document_types`, and multiple client/facility codes); the UI
sends single-value lists for client and facility.

Because commonDb spans all clients, matches are grouped by `client_code` and each
client's webdb is queried separately. A client code with no corresponding
`CAPC_APIGATEWAY_*` database is reported in the status line under
`clients_skipped` rather than failing the search.

### "All clients" is an explicit choice, and an unreliable one

**Client code** and **Facility code** are both required. Leaving either blank is a
form error, not a shortcut for "everything" — searching every client or every
facility means picking the explicit **All clients** / **All facilities** entry at
the top of the dropdown. Choosing "All clients" also pins Facility to "All
facilities" and disables it, since facility codes only mean anything within one
client.

The UI sends `client_codes: []` / `facility_codes: []` for those entries, so the
API contract is unchanged — an empty list still means "no restriction" there. The
requirement is a UI guard, deliberately, because the unrestricted search is the
one whose results mislead:

The result is capped at `MAX_SEARCH_ROWS` (5000 in `.env`)
and the query carries no `ORDER BY`, deliberately: sorting on the unindexed
`service_date` forced a filesort that exhausted the server's temp disk. So the
cap cuts wherever the scan happened to stop, not at "the newest 5000".

A real production run with only `date_range: last_7_days` and no client returned:

```
metadata_match_count : 5000      <- exactly the cap
truncated            : true
clients_matched      : ['AHF', 'CHS', 'CHS_HOSPITAL']   <- 3 of 17
```

Three clients, not seventeen, and nothing about the row count hints at that. The
`truncated` flag is the only reliable signal, and the UI now reports how many
clients a truncated result actually covered. Treat any truncated search as a
sample, never as "all the data for this date range" — narrow by client or
facility and run it per client instead.

commonDb splits some clients into service lines that webdb does not; their
documents live in the parent client's database. `CLIENT_CODE_ALIASES` in
`app/metadata_service.py` maps them:

| commonDb `client_code` | webdb database |
| --- | --- |
| `CHS_ED` | `CAPC_APIGATEWAY_CHS` |
| `CHS_HOSPITAL` | `CAPC_APIGATEWAY_CHS` |
| `PH_ANESTHESIA` | `CAPC_APIGATEWAY_PH` |

Aliases collapse into their parent before the lookup, so a search matching `CHS`,
`CHS_ED` and `CHS_HOSPITAL` makes one query against `CAPC_APIGATEWAY_CHS` rather
than three. Add new service lines to that dict.

Corresponding API: `POST /api/metadata/search` and `POST /api/metadata/download/start`.

### Direct client lookup

The original behaviour: pick one client, then search by account number or
encounter ID (comma, space, or newline separated). `POST /api/search`.

Select one or more output file types, search, then download the matching files as a ZIP.

**The two boxes are combined with `AND`, not `OR`.** Each filter is appended as
another `AND <column> IN (…)`, so an encounter has to belong to one of the
accounts listed as well as being in the encounter list. Against production, for
client `CHS`:

| Account numbers | Encounter IDs | Result |
| --- | --- | --- |
| `60768137` | *(empty)* | 1 row |
| *(empty)* | `37435765` | 1 row |
| `60768137` | `37435765` — same encounter | 1 row |
| `60768137` | `37435770` — a different encounter | **0 rows** |

So pasting a list of accounts alongside a list of unrelated encounter IDs returns
nothing at all. To look up both sets, search them one box at a time. An empty
result with both boxes filled is called out explicitly in the status line rather
than being reported as a flat "0 encounters".

## commonDb

`overall_data` is metadata only — it holds no S3 paths. Its job is to answer
"which encounters do you mean?"; the file locations still come from the client
database. The two are linked on `encounter_id`.

commonDb is a single shared company server with no production/staging split, so
it is configured with one set of `COMMON_DB_*` values (see `.env.example`). The
Environment dropdown still selects which webdb the file paths are read from.

Note that `service_date` is **not** indexed on `overall_data`, so a search filtered
only by a wide date range has to scan the table and may be slow. Adding a client or
facility narrows it considerably.

## File outputs

Each result can carry several S3 document outputs. Available types:

- PCS XML result, E/M request/result, CPT request/result, CMCS result (encounter-level)
- Original document and PDF document (`document_mst.path` and `document_mst.orignal_path`, per document)

**At least one type is required.** An empty selection used to mean "everything",
which quietly pulled every file an encounter has; search and download now both
reject it (`400`, and the UI blocks the click) so the outputs are always an
explicit choice.

The two document columns are stored **relative to the `ezcapc/` root**, unlike the
`*_s3_path` processing columns which already contain it. `s3_key()` in
`app/download_service.py` prepends `ezcapc/` to them — the same thing the download
notebooks do by hand (`final['orignal_path'] = 'ezcapc/' + final['orignal_path']`)
— so without it the PDF and original-document fetches 404. A value that already
carries the prefix, or that is stored as a full `s3://bucket/…` URI, is normalised
first and not prefixed twice.

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
  original/PDF documents) are all kept. An object that can't be fetched is counted under
  "skipped" and recorded as a `FAILED_*.txt` note in the ZIP holding the column, the exact
  key that was requested, and the error; the same line is written to the server log.

Folder structure is selectable: **One folder** (`encounter_account/…`, the default) or
**Facility wise** (`facility/encounter_account/…`).

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