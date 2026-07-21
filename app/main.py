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

from .database import get_client_engine, list_client_codes
from .download_service import ZipProgress, download_totals, zip_documents
from .models import DownloadRequest, SearchRequest
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


def _engine_for(environment: str, client: str):
    if client not in list_client_codes(environment):
        raise HTTPException(status_code=400, detail=f"Unknown client '{client}' for environment '{environment}'.")
    return get_client_engine(environment, client)


@app.post("/api/search")
def search(request: SearchRequest) -> dict[str, object]:
    if not any([request.account_numbers, request.encounter_ids]):
        raise HTTPException(status_code=400, detail="Enter at least one account or encounter filter.")

    rows = search_documents(_engine_for(request.environment, request.client), request)
    encounters = group_by_encounter(rows, request.selected_files)
    return {"count": len(encounters), "document_count": len(rows), "rows": encounters}


def _rows_to_download(request: DownloadRequest) -> list[dict]:
    if not any([request.account_numbers, request.encounter_ids]):
        raise HTTPException(status_code=400, detail="Enter at least one account or encounter filter.")

    rows = search_documents(_engine_for(request.environment, request.client), request)
    if request.result_ids:
        wanted = set(request.result_ids)
        rows = [row for row in rows if row_id(row) in wanted or row.get("id") in wanted]

    if not rows:
        raise HTTPException(status_code=404, detail="No matching rows found to download.")
    return rows


def _run_download_job(job_id: str, job: DownloadJob, request: DownloadRequest, rows: list[dict]) -> None:
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


@app.post("/api/download/start")
def download_start(request: DownloadRequest) -> dict[str, object]:
    rows = _rows_to_download(request)
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