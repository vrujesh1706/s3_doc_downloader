from functools import lru_cache

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from .config import DBEnvironment, get_settings

CLIENT_DB_PREFIX = "CAPC_APIGATEWAY_"
EXCLUDED_CLIENT_CODES = {"SYSTEM", "TEST"}


def _connect_args(env: DBEnvironment) -> dict:
    if env.db_ssl:
        return {"ssl": {"fake_flag_to_enable_tls": True}}
    return {}


@lru_cache(maxsize=8)
def get_engine(environment: str) -> Engine:
    env = get_settings().environment(environment)
    return create_engine(env.database_url(), connect_args=_connect_args(env), pool_pre_ping=True)


@lru_cache(maxsize=64)
def get_client_engine(environment: str, client_code: str) -> Engine:
    env = get_settings().environment(environment)
    db_name = f"{CLIENT_DB_PREFIX}{client_code}"
    return create_engine(env.database_url(db_name), connect_args=_connect_args(env), pool_pre_ping=True)


@lru_cache(maxsize=8)
def list_client_codes(environment: str) -> tuple[str, ...]:
    with get_engine(environment).connect() as connection:
        rows = connection.execute(text("SHOW DATABASES")).fetchall()
    codes = sorted(
        name[len(CLIENT_DB_PREFIX):]
        for (name,) in rows
        if name.startswith(CLIENT_DB_PREFIX)
        and name[len(CLIENT_DB_PREFIX):] not in EXCLUDED_CLIENT_CODES
    )
    return tuple(codes)
