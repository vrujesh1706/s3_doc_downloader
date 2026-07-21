import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")

ENVIRONMENT_PREFIXES = {"production": "PROD", "staging": "STAGING"}


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


@dataclass(frozen=True)
class DBEnvironment:
    name: str
    db_user: str
    db_password: str
    db_host: str
    db_port: int
    db_ssl: bool
    s3_bucket: str

    def database_url(self, db_name: str = "") -> str:
        return f"mysql+pymysql://{self.db_user}:{self.db_password}@{self.db_host}:{self.db_port}/{db_name}"


@dataclass(frozen=True)
class Settings:
    environments: dict[str, DBEnvironment]
    max_search_rows: int
    max_download_files: int

    def environment(self, name: str) -> DBEnvironment:
        try:
            return self.environments[name]
        except KeyError:
            raise ValueError(f"Unknown environment '{name}'. Expected one of {sorted(self.environments)}.")


def _load_environment(name: str, prefix: str) -> DBEnvironment:
    return DBEnvironment(
        name=name,
        db_user=os.getenv(f"{prefix}_DB_USER", ""),
        db_password=os.getenv(f"{prefix}_DB_PASSWORD", ""),
        db_host=os.getenv(f"{prefix}_DB_HOST", ""),
        db_port=_int_env(f"{prefix}_DB_PORT", 3306),
        db_ssl=_bool_env(f"{prefix}_DB_SSL", True),
        s3_bucket=os.getenv(f"{prefix}_S3_BUCKET", ""),
    )


def get_settings() -> Settings:
    environments = {
        name: _load_environment(name, prefix) for name, prefix in ENVIRONMENT_PREFIXES.items()
    }

    missing: list[str] = []
    for name, prefix in ENVIRONMENT_PREFIXES.items():
        env = environments[name]
        for field, value in {
            "DB_USER": env.db_user,
            "DB_PASSWORD": env.db_password,
            "DB_HOST": env.db_host,
            "S3_BUCKET": env.s3_bucket,
        }.items():
            if not value:
                missing.append(f"{prefix}_{field}")

    if missing:
        raise RuntimeError(f"Missing required environment values in .env: {', '.join(missing)}")

    return Settings(
        environments=environments,
        max_search_rows=_int_env("MAX_SEARCH_ROWS", 5000),
        max_download_files=_int_env("MAX_DOWNLOAD_FILES", 2500),
    )
