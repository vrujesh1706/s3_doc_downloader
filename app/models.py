from datetime import date
from typing import Literal

from pydantic import BaseModel, Field


FileColumn = Literal[
    "nlp_pcs_xml_result_s3_path",
    "eandmcs_request_s3_path",
    "eandmcs_result_s3_path",
    "cptcs_request_s3_path",
    "cptcs_result_s3_path",
    "cmcs_result_s3_path",
    "filePath",
    "orignal_path",
]

FolderStructure = Literal["facility_wise", "single_folder"]
Environment = Literal["production", "staging"]

DateRange = Literal[
    "any",
    "today",
    "yesterday",
    "last_7_days",
    "last_30_days",
    "this_week",
    "last_week",
    "this_month",
    "last_month",
]


class MetadataFilters(BaseModel):
    """Filters applied to `commonDb.overall_data` to find encounters.

    `date_from`/`date_to` are inclusive and take precedence over `date_range`.
    """

    date_range: DateRange = "any"
    date_from: date | None = None
    date_to: date | None = None
    client_codes: list[str] = Field(default_factory=list)
    facility_codes: list[str] = Field(default_factory=list)
    account_numbers: list[str] = Field(default_factory=list)
    encounter_ids: list[str] = Field(default_factory=list)
    document_types: list[str] = Field(default_factory=list)


class SearchRequest(BaseModel):
    environment: Environment = "production"
    client: str = Field(min_length=1)
    account_numbers: list[str] = Field(default_factory=list)
    encounter_ids: list[str] = Field(default_factory=list)
    selected_files: list[FileColumn] = Field(default_factory=list)


class DownloadRequest(SearchRequest):
    result_ids: list[str] = Field(default_factory=list)
    folder_structure: FolderStructure = "single_folder"


class MetadataSearchRequest(BaseModel):
    """Search driven by commonDb rather than by an explicit client + id list.

    `environment` still selects which webdb the file paths are read from;
    commonDb itself is a single shared server.
    """

    environment: Environment = "production"
    filters: MetadataFilters = Field(default_factory=MetadataFilters)
    selected_files: list[FileColumn] = Field(default_factory=list)


class MetadataDownloadRequest(MetadataSearchRequest):
    result_ids: list[str] = Field(default_factory=list)
    folder_structure: FolderStructure = "single_folder"
