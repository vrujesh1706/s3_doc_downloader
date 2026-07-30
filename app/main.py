from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass

import uvicorn

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from .config import get_settings
from .database import get_client_engine, get_common_engine, list_client_codes
from .download_service import ZipProgress, download_totals, zip_documents
from .metadata_service import (
    distinct_client_codes,
    facility_options,
    group_encounters_by_client,
    has_any_filter,
    latest_service_date,
    search_overall_data,
    webdb_client_code,
)
from .models import (
    DownloadRequest,
    MetadataDownloadRequest,
    MetadataSearchRequest,
    SearchRequest,
)
from .query_service import FILE_COLUMNS, group_by_encounter, row_id, search_documents

logger = logging.getLogger(__name__)


@dataclass
class DownloadJob:
    encounters_total: int = 0
    encounters_done: int = 0
    files_total: int = 0
    files_done: int = 0
    file_count: int = 0
    skipped_count: int = 0
    status: str = "running"  # running | done | error
    error: str | None = None
    data: bytes | None = None


_jobs: dict[str, DownloadJob] = {}
_jobs_lock = threading.Lock()

app = FastAPI(title="S3 Document Downloader")
app.mount("/static", StaticFiles(directory="app/static"), name="static")


# commonDb is a separate, shared server that is not always up, and the direct
# client lookup does not need it at all. So nothing touches it until the user
# actually opens the "Find by metadata" tab -- there is no startup warm-up -- and
# when it is down the failure is reported as this, not as a 500.
COMMON_DB_UNAVAILABLE = (
    "commonDb is not reachable right now, so metadata search is unavailable. "
    "Use Direct client lookup (search by account number or encounter ID) instead."
)


def _common_db_error(context: str) -> HTTPException:
    logger.exception(context)
    return HTTPException(status_code=503, detail=COMMON_DB_UNAVAILABLE)


@app.exception_handler(SQLAlchemyError)
def sqlalchemy_exception_handler(_, exc: SQLAlchemyError) -> JSONResponse:
    logger.exception("Database error")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Database error: {exc}"},
    )


@app.exception_handler(Exception)
def generic_exception_handler(_, exc: Exception) -> JSONResponse:
    logger.exception("Unexpected server error")
    return JSONResponse(
        status_code=500,
        content={"detail": f"Server error: {exc}"},
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    with open("app/static/index.html", encoding="utf-8") as file_obj:
        return file_obj.read()


@app.get("/api/file-types")
def file_types() -> list[dict[str, str]]:
    return [{"value": value, "label": label} for value, label in FILE_COLUMNS.items()]


@app.get("/api/clients")
def clients(environment: str = "production") -> list[str]:
    return list(list_client_codes(environment))


@app.get("/api/metadata/clients")
def metadata_clients() -> list[str]:
    """Client codes present in commonDb.

    The UI only calls this when the metadata tab is opened, so the page loads
    (and the direct lookup works) without commonDb being reachable at all.
    """
    try:
        return list(distinct_client_codes())
    except SQLAlchemyError as exc:
        raise _common_db_error("Could not load client codes from commonDb") from exc


@app.get("/api/metadata/facilities")
def metadata_facilities(client_code: str) -> list[dict[str, str]]:
    """Facilities of one client, for the dependent dropdown.

    Returned as `{value, label}` because the bare codes are unreadable -- the
    label carries the facility description alongside the code.
    """
    client_code = client_code.strip()
    if not client_code:
        raise HTTPException(status_code=400, detail="A client_code is required.")
    try:
        options = facility_options(client_code)
    except SQLAlchemyError as exc:
        raise _common_db_error("Could not load facility codes from commonDb") from exc

    return [
        {"value": code, "label": f"{code} — {name}" if name and name != code else code}
        for code, name in options
    ]


# An empty `selected_files` used to fall back to "every column", which meant a
# forgotten checkbox pulled every file each encounter has. It is now an error, so
# the set of outputs is always something the caller chose.
def _require_file_types(selected_files: list) -> None:
    if not selected_files:
        raise HTTPException(
            status_code=400,
            detail="Select at least one file output — otherwise every file for every encounter would be included.",
        )


# Accounts and encounters are alternatives, not a combined filter. They are
# applied as `AND account_number IN (...) AND encounter_id IN (...)`, so a list
# of accounts beside a list of unrelated encounters matches nothing -- the two
# have to describe the same encounters to return anything at all. The UI now
# disables one box once the other is used; this keeps direct API callers honest
# too, rather than handing them a silent empty result.
def _require_one_direct_filter(request: SearchRequest) -> None:
    has_accounts = bool(request.account_numbers)
    has_encounters = bool(request.encounter_ids)
    if not (has_accounts or has_encounters):
        raise HTTPException(status_code=400, detail="Enter an account number or an encounter ID.")
    if has_accounts and has_encounters:
        raise HTTPException(
            status_code=400,
            detail=(
                "Search by account number or by encounter ID, not both. They are combined with AND, "
                "so passing both only matches encounters that satisfy each list at once."
            ),
        )


def _engine_for(environment: str, client: str):
    if client not in list_client_codes(environment):
        raise HTTPException(status_code=400, detail=f"Unknown client '{client}' for environment '{environment}'.")
    return get_client_engine(environment, client)


@app.post("/api/search")
def search(request: SearchRequest) -> dict[str, object]:
    _require_file_types(request.selected_files)
    _require_one_direct_filter(request)

    rows = search_documents(_engine_for(request.environment, request.client), request)
    encounters = group_by_encounter(rows, request.selected_files)
    return {"count": len(encounters), "document_count": len(rows), "rows": encounters}


def _resolve_via_common_db(request: MetadataSearchRequest) -> tuple[list[dict], dict[str, object]]:
    """Find encounters in commonDb, then read their file paths from webdb.

    commonDb covers every client in one table, so the matches are split by
    client code and each client's webdb is queried with its own encounter ids.
    A client present in commonDb but with no `CAPC_APIGATEWAY_*` database is
    reported back rather than failing the whole search.
    """
    _require_file_types(request.selected_files)
    try:
        if not has_any_filter(request.filters):
            raise HTTPException(status_code=400, detail="Enter at least one filter before searching.")
        metadata_rows = search_overall_data(get_common_engine(), request.filters)
    except ValueError as exc:
        # Bad filter combinations (e.g. date_from after date_to) are user errors.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except SQLAlchemyError as exc:
        # commonDb unreachable -- say so plainly instead of leaking a 500 with SQL.
        raise _common_db_error("commonDb search failed") from exc

    # Keyed by webdb client, so aliased codes (CHS_ED -> CHS) are already merged
    # into one query per database.
    by_client = group_encounters_by_client(metadata_rows)
    known_clients = set(list_client_codes(request.environment))

    rows: list[dict] = []
    for client_code, encounter_ids in by_client.items():
        if client_code not in known_clients:
            continue
        client_request = SearchRequest(
            environment=request.environment,
            client=client_code,
            encounter_ids=encounter_ids,
            selected_files=request.selected_files,
        )
        rows.extend(search_documents(get_client_engine(request.environment, client_code), client_request))

    # Reported against the commonDb codes the user actually searched, not the
    # webdb databases they were routed to.
    matched_codes = {str(row.get("client_code") or "").strip() for row in metadata_rows}
    matched_codes.discard("")
    skipped_clients = [
        code for code in matched_codes if webdb_client_code(code) not in known_clients
    ]

    summary: dict[str, object] = {
        "metadata_match_count": len(metadata_rows),
        "clients_matched": sorted(matched_codes),
        "clients_skipped": sorted(skipped_clients),
        "webdb_clients_queried": sorted(code for code in by_client if code in known_clients),
        # Hitting the cap means these are an arbitrary subset of the matches,
        # not the newest ones -- the filters need narrowing.
        "truncated": len(metadata_rows) >= get_settings().max_search_rows,
    }

    # An empty result is nearly always "the data does not reach that far
    # forward" -- overall_data lags real time -- so say what the newest matching
    # service date actually is instead of just reporting zero.
    if not metadata_rows:
        try:
            newest = latest_service_date(get_common_engine(), request.filters)
        except SQLAlchemyError:
            logger.exception("Could not determine the newest service date")
            newest = None
        if newest is not None:
            summary["latest_service_date"] = str(newest)

    return rows, summary


@app.post("/api/metadata/search")
def metadata_search(request: MetadataSearchRequest) -> dict[str, object]:
    rows, summary = _resolve_via_common_db(request)
    encounters = group_by_encounter(rows, request.selected_files)
    return {"count": len(encounters), "document_count": len(rows), "rows": encounters, **summary}


def _rows_to_download(request: DownloadRequest) -> list[dict]:
    _require_file_types(request.selected_files)
    _require_one_direct_filter(request)

    rows = search_documents(_engine_for(request.environment, request.client), request)
    if request.result_ids:
        wanted = set(request.result_ids)
        rows = [row for row in rows if row_id(row) in wanted or row.get("id") in wanted]

    if not rows:
        raise HTTPException(status_code=404, detail="No matching rows found to download.")
    return rows


def _run_download_job(
    job_id: str,
    job: DownloadJob,
    request: DownloadRequest | MetadataDownloadRequest,
    rows: list[dict],
) -> None:
    def on_progress(progress: ZipProgress) -> None:
        with _jobs_lock:
            job.encounters_done = progress.encounters_done
            job.encounters_total = progress.encounters_total
            job.files_done = progress.files_done
            job.files_total = progress.files_total
            job.file_count = progress.file_count
            job.skipped_count = progress.skipped_count

    try:
        result = zip_documents(
            rows,
            request.selected_files,
            request.folder_structure,
            request.environment,
            progress_callback=on_progress,
        )
        with _jobs_lock:
            job.file_count = result.file_count
            job.skipped_count = result.skipped_count
            if result.file_count == 0:
                job.status = "error"
                job.error = "No selected S3 files were available for the matching rows."
            else:
                job.data = result.data
                job.status = "done"
    except Exception as exc:  # noqa: BLE001 - surface any failure to the client
        logger.exception("Download job %s failed", job_id)
        with _jobs_lock:
            job.status = "error"
            job.error = str(exc)


def _start_download_job(
    request: DownloadRequest | MetadataDownloadRequest, rows: list[dict]
) -> dict[str, object]:
    encounters_total, files_total = download_totals(
        rows, request.selected_files, request.folder_structure, request.environment
    )

    job_id = uuid.uuid4().hex
    job = DownloadJob(encounters_total=encounters_total, files_total=files_total)
    with _jobs_lock:
        _jobs[job_id] = job

    thread = threading.Thread(
        target=_run_download_job, args=(job_id, job, request, rows), daemon=True
    )
    thread.start()
    return {"job_id": job_id, "encounters_total": encounters_total, "files_total": files_total}


@app.post("/api/download/start")
def download_start(request: DownloadRequest) -> dict[str, object]:
    return _start_download_job(request, _rows_to_download(request))


@app.post("/api/metadata/download/start")
def metadata_download_start(request: MetadataDownloadRequest) -> dict[str, object]:
    rows, _summary = _resolve_via_common_db(request)
    if request.result_ids:
        wanted = set(request.result_ids)
        rows = [row for row in rows if row_id(row) in wanted or row.get("id") in wanted]
    if not rows:
        raise HTTPException(status_code=404, detail="No matching rows found to download.")
    return _start_download_job(request, rows)


@app.get("/api/download/progress/{job_id}")
def download_progress(job_id: str) -> dict[str, object]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown or expired download job.")
        payload = {
            "status": job.status,
            "encounters_done": job.encounters_done,
            "encounters_total": job.encounters_total,
            "files_done": job.files_done,
            "files_total": job.files_total,
            "file_count": job.file_count,
            "skipped_count": job.skipped_count,
            "error": job.error,
        }
        # Errored jobs will not be fetched; drop them so the store stays small.
        if job.status == "error":
            _jobs.pop(job_id, None)
    return payload


@app.get("/api/download/file/{job_id}")
def download_file(job_id: str) -> Response:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Unknown or expired download job.")
        if job.status != "done" or job.data is None:
            raise HTTPException(status_code=409, detail="Download is not ready yet.")
        data = job.data
        file_count = job.file_count
        skipped_count = job.skipped_count
        _jobs.pop(job_id, None)

    headers = {
        "Content-Disposition": 'attachment; filename="s3_document_downloads.zip"',
        "X-Downloaded-Files": str(file_count),
        "X-Skipped-Files": str(skipped_count),
    }
    return Response(content=data, media_type="application/zip", headers=headers)


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )