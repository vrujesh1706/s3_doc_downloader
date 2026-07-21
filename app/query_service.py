from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from .config import get_settings
from .models import FileColumn, SearchRequest


FILE_COLUMNS: dict[str, str] = {
    "nlp_pcs_xml_result_s3_path": "PCS XML result",
    "eandmcs_request_s3_path": "E/M request",
    "eandmcs_result_s3_path": "E/M result",
    "cptcs_request_s3_path": "CPT request",
    "cptcs_result_s3_path": "CPT result",
    "cmcs_result_s3_path": "CMCS result",
    "filePath": "Original document",
    "orignal_path": "PDF document",
}

INNER_SQL = """
SELECT
    am.id AS account_pk,
    am.account_number,
    epm.encounter_id,
    am.client_id,
    am.facility_id,
    dm.service_date,
    dpd.nlp_pcs_xml_result_s3_path,
    epm.eandmcs_request_s3_path,
    epm.eandmcs_result_s3_path,
    epm.cptcs_request_s3_path,
    epm.cptcs_result_s3_path,
    epm.cmcs_result_s3_path,
    dm.path AS filePath,
    dm.orignal_path,
    ROW_NUMBER() OVER (
        PARTITION BY am.id
        ORDER BY dm.created_date DESC, dm.id DESC
    ) AS account_rank
FROM account_mst am
INNER JOIN encounter_mst em
    ON em.account_id = am.id
INNER JOIN document_mst dm
    ON am.id = dm.account_id
INNER JOIN encounter_processing_detail epd
    ON epd.document_id = dm.id
INNER JOIN encounter_processing_mst epm
    ON epm.id = epd.encounter_processing_id
INNER JOIN document_processing_detail dpd
    ON dpd.document_id = dm.id
WHERE
    am.is_active = 1    
    AND em.is_active = 1
    AND dpd.id = (
        SELECT MAX(dpd_latest.id)
        FROM document_processing_detail dpd_latest
        WHERE dpd_latest.document_id = dm.id
    )
    AND epm.id = (
        SELECT MAX(epm_latest.id)
        FROM encounter_processing_mst epm_latest
        WHERE epm_latest.encounter_id = em.id
    )
"""


def normalize_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in seen:
            cleaned.append(item)
            seen.add(item)
    return cleaned


def selected_file_columns(selected_files: list[FileColumn]) -> list[str]:
    columns = [column for column in selected_files if column in FILE_COLUMNS]
    return columns or list(FILE_COLUMNS)


def row_id(row: dict[str, Any]) -> str:
    raw = "|".join(
        str(row.get(name, ""))
        for name in ("account_number", "encounter_id", "client_id", "facility_id", "service_date")
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def count_available_files(row: dict[str, Any], selected_files: list[FileColumn]) -> int:
    return sum(1 for column in selected_file_columns(selected_files) if row.get(column))


def _present_value(value: Any) -> str | None:
    if value is None:
        return None
    item = str(value).strip()
    if not item or item.lower() in {"nan", "none", "null"}:
        return None
    return item


def group_by_encounter(
    rows: list[dict[str, Any]], selected_files: list[FileColumn]
) -> list[dict[str, Any]]:
    """Collapse per-document rows into one row per encounter.

    Each search row is a single document, so a multi-document encounter shows up
    as several rows that share the same encounter-level id. This groups them and,
    for every selected file column, counts the *distinct* files that would end up
    in the download folder (matching the per-folder dedup in the zip step) -- e.g.
    one shared CPT request JSON but five unique PCS XML results.
    """
    columns = selected_file_columns(selected_files)
    groups: dict[str, dict[str, Any]] = {}
    order: list[str] = []

    for row in rows:
        key = row.get("id") or row_id(row)
        group = groups.get(key)
        if group is None:
            group = {
                "id": key,
                "account_number": row.get("account_number"),
                "encounter_id": row.get("encounter_id"),
                "client_id": row.get("client_id"),
                "facility_id": row.get("facility_id"),
                "service_date": row.get("service_date"),
                "document_count": 0,
                "_seen": {column: set() for column in columns},
            }
            groups[key] = group
            order.append(key)

        group["document_count"] += 1
        for column in columns:
            present = _present_value(row.get(column))
            if present is not None:
                group["_seen"][column].add(present)

    output: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        seen: dict[str, set[str]] = group.pop("_seen")
        breakdown = [
            {"column": column, "label": FILE_COLUMNS[column], "count": len(seen[column])}
            for column in columns
        ]
        group["file_breakdown"] = breakdown
        group["download_file_count"] = sum(item["count"] for item in breakdown)
        group["is_multidoc"] = group["document_count"] > 1
        output.append(group)
    return output


def search_documents(engine: Engine, request: SearchRequest) -> list[dict[str, Any]]:
    settings = get_settings()
    filters: list[str] = []
    # No user-facing result limit; cap at max_search_rows as a safety bound only.
    params: dict[str, Any] = {"limit": settings.max_search_rows}

    filter_map = [
        ("account_numbers", "am.account_number", normalize_values(request.account_numbers)),
        ("encounter_ids", "epm.encounter_id", normalize_values(request.encounter_ids)),
    ]

    for param_name, sql_column, values in filter_map:
        if values:
            filters.append(f"AND {sql_column} IN :{param_name}")
            params[param_name] = values

    inner_sql = INNER_SQL + "\n".join(filters)
    sql = (
        f"SELECT * FROM ({inner_sql}) ranked\n"
        "ORDER BY ranked.account_rank ASC, ranked.service_date DESC\n"
        "LIMIT :limit"
    )
    statement = text(sql)
    for param_name, _, values in filter_map:
        if values:
            statement = statement.bindparams(bindparam(param_name, expanding=True))

    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(statement, params).mappings()]

    output: list[dict[str, Any]] = []
    for row in rows:
        row.pop("account_pk", None)
        row.pop("account_rank", None)
        row["id"] = row_id(row)
        row["available_file_count"] = count_available_files(row, request.selected_files)
        output.append(row)
    return output
