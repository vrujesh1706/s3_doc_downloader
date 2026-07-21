from __future__ import annotations

import io
import posixpath
import re
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

import boto3

from .config import get_settings
from .models import FolderStructure
from .query_service import selected_file_columns

# Number of S3 objects fetched concurrently while building the ZIP. The network
# is the bottleneck, so fetching in parallel is much faster than one-at-a-time;
# the ZIP itself is still written from a single thread (zipfile isn't thread-safe).
DOWNLOAD_CONCURRENCY = 8


@dataclass(frozen=True)
class ZipResult:
    data: bytes
    file_count: int
    skipped_count: int


@dataclass(frozen=True)
class ZipProgress:
    encounters_done: int
    encounters_total: int
    files_done: int
    files_total: int
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
    if column in {"filePath", "orignal_path"} and key and not key.startswith("ezcapc/"):
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


@dataclass(frozen=True)
class DownloadItem:
    encounter: str
    folder: str
    key: str
    keys: list[str]
    column: str


def collect_download_items(
    rows: list[dict[str, Any]],
    file_columns: list[str],
    bucket: str,
    folder_structure: FolderStructure,
) -> list[DownloadItem]:
    """Flatten rows into the distinct files that will land in the ZIP.

    Encounter-level columns (e.g. the *_request/_result JSONs) repeat the same S3
    path on every document row of a multi-document encounter, so we dedup per
    folder: the ZIP keeps every unique per-document file but only one copy of each
    shared encounter-level file. This is also what makes the totals known up front,
    so download progress can be reported per encounter (with files as the smooth
    underlying measure).
    """
    seen_keys: set[tuple[str, str]] = set()
    items: list[DownloadItem] = []
    for row in rows:
        encounter = str(row.get("id") or row.get("encounter_id") or "")
        folder = output_folder(row, folder_structure)
        for column in file_columns:
            if is_blank(row.get(column)):
                continue
            keys = candidate_s3_keys(column, row[column], bucket)
            key = keys[0]
            dedup_id = (folder, key)
            if dedup_id in seen_keys:
                continue
            seen_keys.add(dedup_id)
            items.append(
                DownloadItem(encounter=encounter, folder=folder, key=key, keys=keys, column=column)
            )
    return items


def download_totals(
    rows: list[dict[str, Any]],
    selected_files: list[str],
    folder_structure: FolderStructure = "facility_wise",
    environment: str = "production",
) -> tuple[int, int]:
    """Return (encounters_total, files_total) for the pending download."""
    settings = get_settings()
    env = settings.environment(environment)
    file_columns = selected_file_columns(selected_files)
    items = collect_download_items(rows, file_columns, env.s3_bucket, folder_structure)
    encounters = len({item.encounter for item in items})
    return encounters, len(items)


def _fetch_object(
    s3: Any, bucket: str, item: DownloadItem, zip_name: str, original_name: str
) -> tuple[DownloadItem, str, str, bytes | None, Exception | None]:
    """Fetch one S3 object's bytes, trying each candidate key. Runs on a worker
    thread, so it only does network I/O and never touches the shared ZIP."""
    last_error: Exception | None = None
    for candidate_key in item.keys:
        try:
            obj = s3.get_object(Bucket=bucket, Key=candidate_key)
            return item, zip_name, original_name, obj["Body"].read(), None
        except Exception as exc:  # noqa: BLE001 - try the next candidate key
            last_error = exc
    return item, zip_name, original_name, None, last_error


def zip_documents(
    rows: list[dict[str, Any]],
    selected_files: list[str],
    folder_structure: FolderStructure = "facility_wise",
    environment: str = "production",
    progress_callback: Callable[[ZipProgress], None] | None = None,
) -> ZipResult:
    settings = get_settings()
    env = settings.environment(environment)
    bucket = env.s3_bucket
    s3 = boto3.client("s3")
    file_columns = selected_file_columns(selected_files)

    items = collect_download_items(rows, file_columns, bucket, folder_structure)
    files_total = len(items)
    encounters_total = len({item.encounter for item in items})

    # Reserve every ZIP entry name up front, in stable item order, so that
    # parallel (out-of-order) completion still produces deterministic names.
    used_names: set[str] = set()
    plan: list[tuple[DownloadItem, str, str]] = []
    for item in items:
        original_name = posixpath.basename(item.key) or f"{item.column}.dat"
        zip_name = unique_zip_name(f"{item.folder}/{original_name}", used_names)
        plan.append((item, zip_name, original_name))

    to_fetch = plan[: settings.max_download_files]
    capped = plan[settings.max_download_files :]

    zip_buffer = io.BytesIO()
    file_count = 0
    skipped_count = 0
    files_done = 0
    encounters_done = 0
    encounters_remaining: Counter[str] = Counter(item.encounter for item in items)

    def mark_item_done(item: DownloadItem) -> None:
        nonlocal files_done, encounters_done
        files_done += 1
        encounters_remaining[item.encounter] -= 1
        if encounters_remaining[item.encounter] == 0:
            encounters_done += 1

    def report() -> None:
        if progress_callback is not None:
            progress_callback(
                ZipProgress(
                    encounters_done=encounters_done,
                    encounters_total=encounters_total,
                    files_done=files_done,
                    files_total=files_total,
                    file_count=file_count,
                    skipped_count=skipped_count,
                )
            )

    report()
    # Fetch objects concurrently; write them into the archive from this thread as
    # each fetch completes (zipfile is single-threaded, S3 GETs are not).
    with zipfile.ZipFile(zip_buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        if to_fetch:
            with ThreadPoolExecutor(max_workers=DOWNLOAD_CONCURRENCY) as executor:
                futures = [
                    executor.submit(_fetch_object, s3, bucket, item, zip_name, original_name)
                    for item, zip_name, original_name in to_fetch
                ]
                for future in as_completed(futures):
                    item, zip_name, original_name, data, error = future.result()
                    if error is None and data is not None:
                        archive.writestr(zip_name, data)
                        file_count += 1
                    else:
                        skipped_count += 1
                        failed_name = unique_zip_name(
                            f"{item.folder}/FAILED_{original_name}.txt", used_names
                        )
                        archive.writestr(failed_name, f"{item.key}\n{error}\n")
                    mark_item_done(item)
                    report()

        # Files past the safety cap are not downloaded, just accounted for.
        for item, _zip_name, _original_name in capped:
            skipped_count += 1
            mark_item_done(item)
            report()

    return ZipResult(data=zip_buffer.getvalue(), file_count=file_count, skipped_count=skipped_count)
