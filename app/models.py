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


class SearchRequest(BaseModel):
    environment: Environment = "production"
    client: str = Field(min_length=1)
    account_numbers: list[str] = Field(default_factory=list)
    encounter_ids: list[str] = Field(default_factory=list)
    selected_files: list[FileColumn] = Field(default_factory=list)


class DownloadRequest(SearchRequest):
    result_ids: list[str] = Field(default_factory=list)
    folder_structure: FolderStructure = "facility_wise"
