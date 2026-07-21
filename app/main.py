from __future__ import annotations

import logging
import uvicorn

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError

from .database import get_client_engine, list_client_codes
from .download_service import zip_documents
from .models import DownloadRequest, SearchRequest
from .query_service import FILE_COLUMNS, row_id, search_documents

logger = logging.getLogger(__name__)

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
    return {"count": len(rows), "rows": rows}


@app.post("/api/download")
def download(request: DownloadRequest) -> Response:
    if not any([request.account_numbers, request.encounter_ids]):
        raise HTTPException(status_code=400, detail="Enter at least one account or encounter filter.")

    rows = search_documents(_engine_for(request.environment, request.client), request)
    if request.result_ids:
        wanted = set(request.result_ids)
        rows = [row for row in rows if row_id(row) in wanted or row.get("id") in wanted]

    if not rows:
        raise HTTPException(status_code=404, detail="No matching rows found to download.")

    result = zip_documents(rows, request.selected_files, request.folder_structure, request.environment)
    if result.file_count == 0:
        raise HTTPException(status_code=404, detail="No selected S3 files were available for the matching rows.")

    headers = {
        "Content-Disposition": 'attachment; filename="s3_document_downloads.zip"',
        "X-Downloaded-Files": str(result.file_count),
        "X-Skipped-Files": str(result.skipped_count),
    }
    return Response(content=result.data, media_type="application/zip", headers=headers)


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )