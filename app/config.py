import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL


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
class CommonDB:
    """commonDb, which holds `overall_data` -- the metadata table used to find
    encounters. It is a single company server shared by all environments, so
    unlike the per-client databases it has no production/staging split."""

    user: str
    password: str
    host: str
    port: int
    db_name: str

    def database_url(self) -> URL:
        # Built with URL.create rather than an f-string so that credentials
        # containing URL-significant characters (@, :, /, #) are escaped.
        return URL.create(
            "mysql+pymysql",
            username=self.user,
            password=self.password,
            host=self.host,
            port=self.port,
            database=self.db_name,
        )


@dataclass(frozen=True)
class DBEnvironment:
    name: str
    db_user: str
    db_password: str
    db_host: str
    db_port: int
    db_ssl: bool
    s3_bucket: str

    def database_url(self, db_name: str = "") -> URL:
        return URL.create(
            "mysql+pymysql",
            username=self.db_user,
            password=self.db_password,
            host=self.db_host,
            port=self.db_port,
            database=db_name,
        )


@dataclass(frozen=True)
class Settings:
    environments: dict[str, DBEnvironment]
    common_db: CommonDB
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

    common_db = CommonDB(
        user=os.getenv("COMMON_DB_USER", ""),
        password=os.getenv("COMMON_DB_PASSWORD", ""),
        host=os.getenv("COMMON_DB_HOST", ""),
        port=_int_env("COMMON_DB_PORT", 3306),
        db_name=os.getenv("COMMON_DB_NAME", ""),
    )
    for field, value in {
        "COMMON_DB_USER": common_db.user,
        "COMMON_DB_HOST": common_db.host,
        "COMMON_DB_NAME": common_db.db_name,
    }.items():
        if not value:
            missing.append(field)

    if missing:
        raise RuntimeError(f"Missing required environment values in .env: {', '.join(missing)}")

    return Settings(
        environments=environments,
        common_db=common_db,
        max_search_rows=_int_env("MAX_SEARCH_ROWS", 5000),
        max_download_files=_int_env("MAX_DOWNLOAD_FILES", 2500),
    )
