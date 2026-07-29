"""Encounter lookup against `commonDb.overall_data`.

`overall_data` is the metadata layer: one row per processed encounter, covering
every client, with the business fields people actually search by (service date,
client, facility, account, document type). It holds no S3 paths -- its job is to
answer "which encounters do you mean?". The resolved encounter ids are then
handed to the per-client webdb query, which is what knows the file locations.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

from .config import get_settings
from .database import get_common_engine
from .models import MetadataFilters

TABLE = "overall_data"

# How far back commonDb is queried, in months.
#
# Three, because webdb only retains the last three months of documents -- an
# older encounter has no files left to download, so there is nothing to gain by
# finding it. It is also what makes these queries viable: overall_data holds
# ~17M rows and neither `client_code` nor `service_date` is indexed, so an
# unbounded scan takes minutes. `month` *is* indexed, so bounding on it keeps
# both the dropdowns and the search fast.
#
# `month` is the coding month (it tracks last_coding_date), not the month of
# service -- which is the right axis here, since it matches how webdb retains
# documents. It is *not* interchangeable with service_date: within a single
# `month` the service dates span years, so a service-date filter is still
# applied separately and directly.
MONTH_WINDOW = 3


def recent_months(count: int, today: date | None = None) -> list[str]:
    """The last `count` months as 'YYYY-MM', newest first."""
    today = today or date.today()
    months: list[str] = []
    year, month = today.year, today.month
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return months

# Deliberately just the two columns used downstream: the encounter to look up and
# the client whose database to look it up in. Everything displayed in the results
# table comes from webdb, so pulling more from here is pure cost -- and expensive
# cost, because the wide varchar(500) columns made MySQL's temp rows large enough
# to exhaust the server's disk on big clients.
SELECT_COLUMNS = ("encounter_id", "client_code")


def resolve_date_range(
    filters: MetadataFilters, today: date | None = None
) -> tuple[date | None, date | None] | None:
    """Turn either a named range ("last week") or explicit dates into (start, end).

    Both ends are inclusive calendar dates. Explicit `date_from`/`date_to` win
    over `date_range`, and they must be given as a pair. A one-sided range used to
    be allowed, which made "Last 7 days" plus a From date silently widen the
    search instead of narrowing it: the preset was dropped and the open end ran to
    the edge of the retention window.
    """
    today = today or date.today()

    if filters.date_from or filters.date_to:
        if not (filters.date_from and filters.date_to):
            missing = "To" if filters.date_from else "From"
            raise ValueError(
                f"Enter both From and To -- {missing} is missing. A one-sided date range is not "
                "allowed; clear both to use the Service date range instead."
            )
        if filters.date_from > filters.date_to:
            raise ValueError("date_from cannot be after date_to.")
        return filters.date_from, filters.date_to

    preset = filters.date_range
    if preset in (None, "any"):
        return None

    if preset == "today":
        return today, today
    if preset == "yesterday":
        return today - timedelta(days=1), today - timedelta(days=1)
    if preset == "last_7_days":
        return today - timedelta(days=6), today
    if preset == "last_30_days":
        return today - timedelta(days=29), today
    if preset == "this_week":
        start = today - timedelta(days=today.weekday())
        return start, today
    if preset == "last_week":
        # Previous Monday-Sunday, not a rolling 7 days.
        this_week_start = today - timedelta(days=today.weekday())
        return this_week_start - timedelta(days=7), this_week_start - timedelta(days=1)
    if preset == "this_month":
        return today.replace(day=1), today
    if preset == "last_month":
        this_month_start = today.replace(day=1)
        last_month_end = this_month_start - timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end

    raise ValueError(f"Unknown date range '{preset}'.")


def normalize_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        item = str(value).strip()
        if item and item not in seen:
            cleaned.append(item)
            seen.add(item)
    return cleaned


@dataclass(frozen=True)
class _Condition:
    sql: str
    param: str
    value: Any
    expanding: bool = False


def _build_conditions(filters: MetadataFilters, include_dates: bool = True) -> list[_Condition]:
    conditions: list[_Condition] = []

    date_range = resolve_date_range(filters) if include_dates else None
    if date_range is not None:
        start, end = date_range
        if start is not None:
            conditions.append(_Condition("service_date >= :date_from", "date_from", start))
        if end is not None:
            # service_date is a datetime, so the end day is bounded exclusively
            # at the next midnight -- otherwise everything after 00:00 on the
            # last day would be dropped.
            conditions.append(
                _Condition(
                    "service_date < :date_to_exclusive", "date_to_exclusive", end + timedelta(days=1)
                )
            )

    exact_filters = [
        ("client_codes", "client_code", normalize_values(filters.client_codes)),
        ("facility_codes", "facility_code", normalize_values(filters.facility_codes)),
        ("account_numbers", "account_number", normalize_values(filters.account_numbers)),
        ("encounter_ids", "encounter_id", normalize_values(filters.encounter_ids)),
    ]
    for param, column, values in exact_filters:
        if values:
            conditions.append(_Condition(f"{column} IN :{param}", param, values, expanding=True))

    # Document type can be recorded as either a code or a free-text type, and
    # people search with whichever they know, so match against both.
    doc_types = normalize_values(filters.document_types)
    if doc_types:
        conditions.append(
            _Condition(
                "(enc_doc_code IN :document_types OR enc_doc_type IN :document_types)",
                "document_types",
                doc_types,
                expanding=True,
            )
        )

    return conditions


def has_any_filter(filters: MetadataFilters) -> bool:
    return bool(_build_conditions(filters))


def search_overall_data(engine: Engine, filters: MetadataFilters) -> list[dict[str, Any]]:
    """Return matching encounter metadata rows from `overall_data`."""
    settings = get_settings()
    conditions = _build_conditions(filters)
    if not conditions:
        raise ValueError("Enter at least one filter before searching commonDb.")

    # Bound every search to the retention window. This is what lets the query use
    # the `month` index instead of scanning 17M rows, and it costs nothing: older
    # encounters have no documents left in webdb to download. Added here rather
    # than in _build_conditions so it never counts as a user-supplied filter.
    conditions = [
        _Condition("month IN :months", "months", recent_months(MONTH_WINDOW), expanding=True),
        *conditions,
    ]

    params: dict[str, Any] = {condition.param: condition.value for condition in conditions}
    params["limit"] = settings.max_search_rows

    where_sql = "\n  AND ".join(condition.sql for condition in conditions)
    # No ORDER BY on purpose. `service_date` is not indexed, so ordering by it
    # forces MySQL to materialise and sort every matching row before applying the
    # LIMIT -- on a large client that filesort exhausted the server's temp disk
    # ("No space left on device"). Without it the rows stream out and the LIMIT
    # stops the scan early. The cap is only a safety bound anyway: if it is hit
    # the answer is to narrow the filters, which `truncated` signals.
    sql = (
        f"SELECT {', '.join(SELECT_COLUMNS)}\n"
        f"FROM {TABLE}\n"
        f"WHERE {where_sql}\n"
        "LIMIT :limit"
    )

    statement = text(sql)
    for condition in conditions:
        if condition.expanding:
            statement = statement.bindparams(bindparam(condition.param, expanding=True))

    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement, params).mappings()]


# The two dropdowns are filled from two different tables, because asking
# `overall_data` for both at once is what made this slow. `client_code` sits in
# the `uk_overall_month_client_enc` index alongside `month`, so the client query
# is answered from the index alone. `facility_code` is *not* in that index, so
# adding it forced a 5.6M-row scan of the table itself -- ~35s against ~3s.
#
# Facilities therefore come from commonDb's per-client rollup tables: one table
# per client code, lowercased (`chs`, `chs_ed`, `scp`, ...), holding a monthly
# row per facility. They are tiny -- hundreds to a few thousand rows against
# 17M -- so a facility list costs ~0.1s, and they agree with `overall_data`
# almost exactly: across all 17 clients they miss nothing and offer only 9
# facilities that have no encounters in the window.
#
# `client_facility_info` (a 1.5k-row reference table of every (client, facility)
# pair) is kept only as a fallback for a client with no rollup table yet. It is
# not the primary source because it records mappings rather than activity: it
# lists 590 facilities with no recent encounters, and worse, maps some to the
# wrong client -- e.g. `4695_ED` appears under both `CHS` and `CHS_ED`, though
# in `overall_data` every `_ED` facility belongs solely to `CHS_ED`. That put a
# facility in the CHS dropdown that could only ever return no rows.
#
# All of these are cached for the life of the process; restart the app to pick
# up a brand new client or facility.
FACILITY_TABLE = "client_facility_info"


def _window_start(today: date | None = None) -> date:
    """First day of the oldest month in the retention window."""
    oldest = recent_months(MONTH_WINDOW, today)[-1]
    return date(int(oldest[:4]), int(oldest[5:]), 1)


@lru_cache(maxsize=1)
def _table_names() -> frozenset[str]:
    """Every table in commonDb, lowercased.

    A client's rollup table is named after the client code, so the name has to be
    interpolated into the SQL rather than bound as a parameter. This whitelist is
    what makes that safe: a name is only ever used if it came back from here.
    """
    with get_common_engine().connect() as connection:
        return frozenset(str(name).lower() for (name,) in connection.execute(text("SHOW TABLES")))


@lru_cache(maxsize=1)
def distinct_client_codes() -> tuple[str, ...]:
    """Every client code with encounters in the retention window."""
    statement = text(
        f"SELECT DISTINCT client_code FROM {TABLE} "
        "WHERE month IN :months AND client_code IS NOT NULL AND client_code <> ''"
    ).bindparams(bindparam("months", expanding=True))

    with get_common_engine().connect() as connection:
        rows = connection.execute(statement, {"months": recent_months(MONTH_WINDOW)}).fetchall()

    return tuple(sorted({str(code).strip() for (code,) in rows if str(code).strip()}))


@lru_cache(maxsize=1)
def client_facility_map() -> dict[str, tuple[tuple[str, str], ...]]:
    """Every client in `client_facility_info`, mapped to (code, name) facilities.

    Note the columns here are `client` and `facility_name`, against `client_code`
    and `facility_description` elsewhere.
    """
    statement = text(
        f"SELECT client, facility_code, MAX(facility_name) FROM {FACILITY_TABLE} "
        "GROUP BY client, facility_code"
    )

    grouped: dict[str, dict[str, str]] = {}
    with get_common_engine().connect() as connection:
        rows = connection.execute(statement).fetchall()
    for client_code, facility_code, facility_name in rows:
        client = str(client_code or "").strip()
        facility = str(facility_code or "").strip()
        if client and facility:
            grouped.setdefault(client, {})[facility] = str(facility_name or "").strip()

    return {
        client: tuple(sorted(facilities.items()))
        for client, facilities in sorted(grouped.items())
    }


@lru_cache(maxsize=64)
def facility_options(client_code: str) -> tuple[tuple[str, str], ...]:
    """Facilities belonging to one client as (code, name) pairs, so the dropdown
    only ever offers facilities that actually pair with the selected client.

    The name matters because the codes alone are unreadable: many are bare
    tablespace numbers, and the odd-looking ones (`2310_Clinic`, `WHS_CAPC`) are
    real facilities that look like junk without their description next to them.

    Read from the client's own rollup table, bounded to the retention window so
    the list reflects recent activity. Clients without a rollup table fall back
    to the `client_facility_info` mapping.
    """
    client_code = client_code.strip()
    table = client_code.lower()
    if table not in _table_names():
        return client_facility_map().get(client_code, ())

    # MAX() because a facility has one row per month and the description can
    # differ between them; any one of them is a fine label.
    statement = text(
        f"SELECT facility_code, MAX(facility_description) FROM {table} "
        "WHERE coding_date >= :start GROUP BY facility_code"
    )
    with get_common_engine().connect() as connection:
        rows = connection.execute(statement, {"start": _window_start()}).fetchall()

    return tuple(
        sorted(
            (str(code).strip(), str(name or "").strip())
            for code, name in rows
            if code and str(code).strip()
        )
    )


def latest_service_date(engine: Engine, filters: MetadataFilters) -> Any:
    """Newest service_date matching everything *except* the date filter.

    Used only to explain an empty result ("nothing that recent -- the newest is
    X"). `service_date` is unindexed, so this is deliberately restricted to
    searches that carry a non-date filter to keep the scan bounded.
    """
    conditions = _build_conditions(filters, include_dates=False)
    if not conditions:
        return None

    conditions = [
        _Condition("month IN :months", "months", recent_months(MONTH_WINDOW), expanding=True),
        *conditions,
    ]
    params = {condition.param: condition.value for condition in conditions}
    where_sql = "\n  AND ".join(condition.sql for condition in conditions)

    statement = text(f"SELECT MAX(service_date) FROM {TABLE}\nWHERE {where_sql}")
    for condition in conditions:
        if condition.expanding:
            statement = statement.bindparams(bindparam(condition.param, expanding=True))

    with engine.connect() as connection:
        return connection.execute(statement, params).scalar()


# commonDb splits some clients into service lines that webdb does not: their
# documents all live in the parent client's CAPC_APIGATEWAY_* database. Without
# this, those encounters are found in commonDb but reported as "skipped -- no
# database here" and no files come back.
CLIENT_CODE_ALIASES = {
    "CHS_ED": "CHS",
    "CHS_HOSPITAL": "CHS",
    "PH_ANESTHESIA": "PH",
}


def webdb_client_code(client_code: str) -> str:
    """The webdb database a commonDb client code's documents live in."""
    return CLIENT_CODE_ALIASES.get(client_code, client_code)


def group_encounters_by_client(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    """Bucket encounter ids by the *webdb* client they must be looked up in.

    `overall_data` spans every client but the file paths live in per-client
    databases, so the resolved encounters have to be split back out before the
    webdb lookup can run. Aliased codes collapse into their parent here, so a
    search matching CHS, CHS_ED and CHS_HOSPITAL makes one query against
    CAPC_APIGATEWAY_CHS rather than three.
    """
    grouped: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}
    for row in rows:
        client = str(row.get("client_code") or "").strip()
        encounter_id = row.get("encounter_id")
        if not client or encounter_id is None:
            continue
        target = webdb_client_code(client)
        value = str(encounter_id)
        if value in seen.setdefault(target, set()):
            continue
        seen[target].add(value)
        grouped.setdefault(target, []).append(value)
    return grouped
