from __future__ import annotations

import io
import posixpath
import re
import zipfile
from dataclasses import dataclass
from typing import Any

import boto3

from .config import get_settings
from .models import FolderStructure
from .query_service import selected_file_columns


@dataclass(frozen=True)
class ZipResult:
    data: bytes
    file_count: int
    skipped_count: int


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return text == "" or text.lower() in {"nan", "none", "null"}


def clean_s3_key(value: Any, bucket: str) -> str:
    key = str(value).strip()
    key = key.replace(f"s3://{bucket}/", "")
    key = key.replace("ezdi-production-bucket/", "")
    key = key.replace("ezdi-staging-bucket/", "")
    return key.lstrip("/")


def candidate_s3_keys(column: str, value: Any, bucket: str) -> list[str]:
    key = clean_s3_key(value, bucket)
    keys = [key]
    if column == "filePath" and key and not key.startswith("ezcapc/"):
        keys.append(f"ezcapc/{key}")
    return keys


def safe_part(value: Any, fallback: str) -> str:
    if is_blank(value):
        return fallback
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value).strip())
    text = text.strip("._-")
    return text or fallback


def facility_folder(row: dict[str, Any]) -> str:
    file_path = str(row.get("filePath") or "")
    parts = file_path.split("/")
    if len(parts) > 2 and parts[2]:
        return safe_part(parts[2], "unknown_facility")
    return safe_part(row.get("facility_id"), "unknown_facility")


def output_folder(row: dict[str, Any], folder_structure: FolderStructure) -> str:
    account_number = safe_part(row.get("account_number"), "unknown_account")
    encounter_id = safe_part(row.get("encounter_id"), "unknown_encounter")
    account_folder = f"{encounter_id}_{account_number}"
    if folder_structure == "single_folder":
        return account_folder
    return f"{facility_folder(row)}/{account_folder}"


def unique_zip_name(zip_name: str, used_names: set[str]) -> str:
    if zip_name not in used_names:
        used_names.add(zip_name)
        return zip_name

    folder, _, filename = zip_name.rpartition("/")
    stem, dot, suffix = filename.rpartition(".")
    if not dot:
        stem = filename
        suffix = ""

    counter = 2
    while True:
        new_filename = f"{stem}_{counter}.{suffix}" if suffix else f"{stem}_{counter}"
        candidate = f"{folder}/{new_filename}" if folder else new_filename
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        counter += 1


def zip_documents(
    rows: list[dict[str, Any]],
    selected_files: list[str],
    folder_structure: FolderStructure = "facility_wise",
    environment: str = "production",
) -> ZipResult:
    settings = get_settings()
    env = settings.environment(environment)
    s3 = boto3.client("s3")
    file_columns = selected_file_columns(selected_files)
    zip_buffer = io.BytesIO()
    file_count = 0
    skipped_count = 0
    used_names: set[str] = set()

    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for row in rows:
            folder = output_folder(row, folder_structure)

            for column in file_columns:
                if file_count >= settings.max_download_files:
                    skipped_count += 1
                    continue
                if is_blank(row.get(column)):
                    skipped_count += 1
                    continue

                keys = candidate_s3_keys(column, row[column], env.s3_bucket)
                key = keys[0]
                original_name = posixpath.basename(key) or f"{column}.dat"
                zip_name = unique_zip_name(f"{folder}/{original_name}", used_names)

                last_error: Exception | None = None
                try:
                    for candidate_key in keys:
                        try:
                            obj = s3.get_object(Bucket=env.s3_bucket, Key=candidate_key)
                            archive.writestr(zip_name, obj["Body"].read())
                            file_count += 1
                            last_error = None
                            break
                        except Exception as exc:
                            last_error = exc
                    if last_error is not None:
                        raise last_error
                except Exception as exc:
                    skipped_count += 1
                    failed_name = unique_zip_name(f"{folder}/FAILED_{original_name}.txt", used_names)
                    archive.writestr(failed_name, f"{key}\n{exc}\n")

    return ZipResult(data=zip_buffer.getvalue(), file_count=file_count, skipped_count=skipped_count)
