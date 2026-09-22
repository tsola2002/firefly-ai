import os
import sys
import time
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.stable_api as stable_api


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

# 1-minute candles
TIMEFRAME = 60

# One complete day contains 1,440 one-minute candles
EXPECTED_CANDLES_PER_DAY = 1440

# IQ Option historical request size
CANDLE_BATCH_SIZE = 500

# Maximum number of historical batches
MAX_BATCHES = 10

# ------------------------------------------------------------
# DATE TO ANALYZE
# ------------------------------------------------------------

REPORT_DATE = "2026-09-21"

# Nigeria / West Africa
# UTC + 1
LOCAL_TIMEZONE = timezone(timedelta(hours=1))

# ------------------------------------------------------------
# SQLITE DATABASE
# ------------------------------------------------------------
# SQLite is a serverless database stored in a single local file.
# ------------------------------------------------------------

DATABASE_FILE = Path(__file__).resolve().parent / "firefly_binary_reports.db"
DATABASE_SCHEMA_VERSION = 1


# ============================================================
# REAL FOREX PAIRS
# ============================================================
#
# IMPORTANT:
#
# These are REAL FOREX assets.
#
# DO NOT add "-OTC".
#
# Example:
#
#     USDJPY
#
# NOT:
#
#     USDJPY-OTC
#
# "Binary" is the option/contract type.
# The underlying price feed is USDJPY.
#
# ============================================================

FOREX_PAIRS = [
    "USDJPY",
    "AUDUSD",
    "EURJPY",
    "AUDJPY",
    "GBPJPY",
    "GBPAUD",
    "GBPCAD",
    "CADJPY",
]


# ============================================================
# VALIDATION
# ============================================================

if not IQ_EMAIL:
    print("ERROR: IQ_EMAIL is missing from .env")
    sys.exit(1)

if not IQ_PASSWORD:
    print("ERROR: IQ_PASSWORD is missing from .env")
    sys.exit(1)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("IQ-REAL-FOREX-REPORT")


# ============================================================
# GLOBAL API
# ============================================================

API = None


# ============================================================
# CONNECT TO IQ OPTION
# ============================================================

def connect_iq_option():
    global API

    logger.info("")
    logger.info("=" * 80)
    logger.info("CONNECTING TO IQ OPTION")
    logger.info("=" * 80)

    try:
        API = IQ_Option(
            IQ_EMAIL,
            IQ_PASSWORD,
        )

        check, reason = API.connect()

        if not check:
            logger.error(
                "IQ Option connection failed: %s",
                reason,
            )
            return False

        logger.info("IQ Option connection successful.")

        # ----------------------------------------------------
        # Use PRACTICE account.
        #
        # This is only the account context.
        # It does NOT change the historical market feed.
        # ----------------------------------------------------

        try:
            API.change_balance("PRACTICE")

            balance = API.get_balance()

            logger.info(
                "Practice balance: $%.2f",
                balance,
            )

        except Exception as exc:
            logger.warning(
                "Could not retrieve practice balance: %s",
                exc,
            )

        return True

    except Exception as exc:
        logger.exception(
            "Connection exception: %s",
            exc,
        )
        return False


# ============================================================
# CHECK REAL FOREX ASSETS
# ============================================================

def check_real_forex_assets():
    logger.info("")
    logger.info("=" * 80)
    logger.info("CHECKING REAL FOREX ASSET MAPPINGS")
    logger.info("=" * 80)

    missing = []

    for pair in FOREX_PAIRS:
        active_id = stable_api.OP_code.ACTIVES.get(pair)

        if active_id is None:
            logger.warning(
                "MISSING: %-12s",
                pair,
            )
            missing.append(pair)
        else:
            logger.info(
                "READY:   %-12s -> %s",
                pair,
                active_id,
            )

    # --------------------------------------------------------
    # If all assets already exist, we are done.
    # --------------------------------------------------------

    if not missing:
        logger.info(
            "All real forex asset mappings are available."
        )
        return True

    # --------------------------------------------------------
    # Request IQ Option initialization data.
    # --------------------------------------------------------

    logger.info("")
    logger.info(
        "Some real forex mappings are missing."
    )
    logger.info(
        "Requesting IQ Option asset initialization..."
    )

    try:
        API.api.api_option_init_all_result = None

        start_time = time.time()

        API.api.get_api_option_init_all()

        while (
            API.api.api_option_init_all_result is None
            and time.time() - start_time < 30
        ):
            time.sleep(0.1)

        result = API.api.api_option_init_all_result

        elapsed = time.time() - start_time

        if result is None:
            logger.error(
                "Asset initialization timed out after %.1f seconds.",
                elapsed,
            )
            return False

        logger.info(
            "Asset initialization received in %.2f seconds.",
            elapsed,
        )

        if not result.get("isSuccessful", False):
            logger.error(
                "IQ Option returned unsuccessful asset initialization."
            )
            return False

        result_data = result.get("result", {})

        # ----------------------------------------------------
        # Search binary + turbo.
        #
        # The real underlying asset may be available through
        # either market structure.
        # ----------------------------------------------------

        for market_type in ("binary", "turbo"):

            market_data = result_data.get(
                market_type,
                {},
            )

            actives = market_data.get(
                "actives",
                {},
            )

            for active_id, active_data in actives.items():

                try:
                    raw_name = active_data.get(
                        "name",
                        "",
                    )

                    # IQ Option names may look like:
                    #
                    #     binary.USDJPY
                    #
                    # or simply:
                    #
                    #     USDJPY

                    if "." in raw_name:
                        asset_name = raw_name.split(
                            ".",
                            1,
                        )[1]
                    else:
                        asset_name = raw_name

                    if asset_name in FOREX_PAIRS:

                        numeric_id = int(active_id)

                        stable_api.OP_code.ACTIVES[
                            asset_name
                        ] = numeric_id

                except Exception:
                    continue

        # ----------------------------------------------------
        # Verify again
        # ----------------------------------------------------

        logger.info("")
        logger.info("REAL FOREX ASSET MAPPINGS")
        logger.info("-" * 80)

        all_found = True

        for pair in FOREX_PAIRS:

            active_id = stable_api.OP_code.ACTIVES.get(pair)

            if active_id is not None:

                logger.info(
                    "READY:   %-12s -> %s",
                    pair,
                    active_id,
                )

            else:

                logger.error(
                    "MISSING: %-12s -> NOT FOUND",
                    pair,
                )

                all_found = False

        logger.info("-" * 80)

        return all_found

    except Exception as exc:

        logger.exception(
            "Real forex asset initialization failed: %s",
            exc,
        )

        return False


# ============================================================
# REPORT DATE RANGE
# ============================================================

def get_report_range():
    """
    Convert the Nigerian local report date into UTC.

    Nigerian local:

        2026-09-07 00:00:00
        ->
        2026-09-08 00:00:00

    UTC:

        2026-09-06 23:00:00
        ->
        2026-09-07 23:00:00
    """

    year, month, day = map(
        int,
        REPORT_DATE.split("-"),
    )

    local_start = datetime(
        year,
        month,
        day,
        0,
        0,
        0,
        tzinfo=LOCAL_TIMEZONE,
    )

    local_end = local_start + timedelta(days=1)

    utc_start = local_start.astimezone(
        timezone.utc
    )

    utc_end = local_end.astimezone(
        timezone.utc
    )

    return (
        local_start,
        local_end,
        utc_start,
        utc_end,
    )


# ============================================================
# GET IQ OPTION SERVER TIME
# ============================================================

def get_server_timestamp():

    try:
        return API.get_server_timestamp()

    except Exception:

        try:
            return int(time.time())

        except Exception:

            return int(
                datetime.now(
                    timezone.utc
                ).timestamp()
            )


# ============================================================
# FETCH HISTORICAL REAL FOREX CANDLES
# ============================================================

def get_historical_candles(pair):
    """
    Retrieve historical 1-minute candles directly from
    IQ Option.

    IMPORTANT:

    This function uses:

        USDJPY

    and NOT:

        USDJPY-OTC
    """

    active_id = stable_api.OP_code.ACTIVES.get(pair)

    if active_id is None:

        logger.error(
            "%s has no IQ Option active ID.",
            pair,
        )

        return []

    (
        local_start,
        local_end,
        utc_start,
        utc_end,
    ) = get_report_range()

    logger.info("")
    logger.info("=" * 80)
    logger.info(
        "FETCHING REAL FOREX DATA: %s",
        pair,
    )
    logger.info("=" * 80)

    logger.info(
        "IQ Option Active ID: %s",
        active_id,
    )

    logger.info(
        "Local period: %s -> %s",
        local_start.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        (
            local_end
            - timedelta(seconds=60)
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    )

    logger.info(
        "UTC period: %s -> %s",
        utc_start.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        (
            utc_end
            - timedelta(seconds=60)
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    )

    # --------------------------------------------------------
    # Start from the END of the requested day.
    # --------------------------------------------------------

    cursor = int(utc_end.timestamp())
    start_timestamp = int(
        utc_start.timestamp()
    )

    all_candles = []
    batch_number = 0

    # --------------------------------------------------------
    # Keep requesting backwards until we reach the start.
    # --------------------------------------------------------

    while cursor > start_timestamp:

        batch_number += 1

        logger.info(
            "Batch %d: requesting up to %d candles...",
            batch_number,
            CANDLE_BATCH_SIZE,
        )

        candles = None

        try:

            candles = API.get_candles(
                pair,
                TIMEFRAME,
                CANDLE_BATCH_SIZE,
                cursor,
            )

        except Exception as exc:

            logger.error(
                "%s candle request failed: %s",
                pair,
                exc,
            )

            # ------------------------------------------------
            # Try one reconnect.
            # ------------------------------------------------

            logger.warning(
                "Attempting IQ Option reconnect..."
            )

            try:

                check, reason = API.connect()

                if not check:

                    logger.error(
                        "Reconnect failed: %s",
                        reason,
                    )

                    break

                time.sleep(2)

                candles = API.get_candles(
                    pair,
                    TIMEFRAME,
                    CANDLE_BATCH_SIZE,
                    cursor,
                )

            except Exception as reconnect_exc:

                logger.error(
                    "Reconnect request failed: %s",
                    reconnect_exc,
                )

                break

        if not candles:

            logger.warning(
                "No candles returned."
            )

            break

        logger.info(
            "Received %d candles.",
            len(candles),
        )

        # ----------------------------------------------------
        # Find oldest candle.
        # ----------------------------------------------------

        oldest_timestamp = None

        for candle in candles:

            try:

                candle_from = float(
                    candle.get(
                        "from",
                        0,
                    )
                )

                candle_to = float(
                    candle.get(
                        "to",
                        candle_from + TIMEFRAME,
                    )
                )

            except Exception:

                continue

            # ------------------------------------------------
            # Keep only candles completely inside the
            # requested UTC period.
            # ------------------------------------------------

            if (
                candle_from >= start_timestamp
                and candle_from < utc_end.timestamp()
                and candle_to <= utc_end.timestamp()
            ):

                all_candles.append(candle)

            if (
                oldest_timestamp is None
                or candle_from < oldest_timestamp
            ):

                oldest_timestamp = candle_from

        # ----------------------------------------------------
        # No valid oldest candle.
        # ----------------------------------------------------

        if oldest_timestamp is None:

            logger.warning(
                "Could not determine oldest candle."
            )

            break

        # ----------------------------------------------------
        # Move backwards.
        # ----------------------------------------------------

        next_cursor = int(
            oldest_timestamp
        ) - 1

        if next_cursor >= cursor:

            logger.warning(
                "Cursor failed to move backwards."
            )

            break

        cursor = next_cursor

        # ----------------------------------------------------
        # Safety limit.
        # ----------------------------------------------------

        if batch_number >= MAX_BATCHES:

            logger.warning(
                "Maximum batch safety limit reached."
            )

            break

    # ========================================================
    # REMOVE DUPLICATES
    # ========================================================

    unique_candles = {}

    for candle in all_candles:

        try:

            candle_from = int(
                float(
                    candle["from"]
                )
            )

            unique_candles[
                candle_from
            ] = candle

        except Exception:

            continue

    # ========================================================
    # SORT CHRONOLOGICALLY
    # ========================================================

    candles = sorted(
        unique_candles.values(),
        key=lambda candle: float(
            candle["from"]
        ),
    )

    logger.info("")
    logger.info(
        "%s TOTAL UNIQUE CANDLES: %d",
        pair,
        len(candles),
    )

    # ========================================================
    # EXPECTED DATA CHECK
    # ========================================================

    if len(candles) == EXPECTED_CANDLES_PER_DAY:

        logger.info(
            "%s: COMPLETE 24-HOUR DATASET.",
            pair,
        )

    elif len(candles) > 0:

        logger.warning(
            "%s: expected approximately %d "
            "candles but received %d.",
            pair,
            EXPECTED_CANDLES_PER_DAY,
            len(candles),
        )

    return candles


# ============================================================
# CANDLE COLOR
# ============================================================

def candle_color(candle):

    open_price = float(
        candle["open"]
    )

    close_price = float(
        candle["close"]
    )

    if close_price > open_price:
        return "GREEN"

    if close_price < open_price:
        return "RED"

    return "DOJI"


# ============================================================
# FIND HIGHEST STREAK DETAILS
# ============================================================

def find_highest_streak_details(candles):

    highest_green = 0
    highest_red = 0

    current_green = 0
    current_red = 0

    current_green_start = None
    current_red_start = None

    highest_green_start = None
    highest_green_end = None

    highest_red_start = None
    highest_red_end = None

    for candle in candles:

        candle_from = float(
            candle["from"]
        )

        candle_to = float(
            candle.get(
                "to",
                candle_from + TIMEFRAME,
            )
        )

        color = candle_color(candle)

        # ====================================================
        # GREEN
        # ====================================================

        if color == "GREEN":

            current_green += 1
            current_red = 0
            current_red_start = None

            if current_green == 1:

                current_green_start = candle_from

            if current_green > highest_green:

                highest_green = current_green

                highest_green_start = (
                    current_green_start
                )

                highest_green_end = candle_to

        # ====================================================
        # RED
        # ====================================================

        elif color == "RED":

            current_red += 1
            current_green = 0
            current_green_start = None

            if current_red == 1:

                current_red_start = candle_from

            if current_red > highest_red:

                highest_red = current_red

                highest_red_start = (
                    current_red_start
                )

                highest_red_end = candle_to

        # ====================================================
        # DOJI
        # ====================================================

        else:

            current_green = 0
            current_red = 0

            current_green_start = None
            current_red_start = None

    return {
        "highest_green": highest_green,
        "green_start": highest_green_start,
        "green_end": highest_green_end,

        "highest_red": highest_red,
        "red_start": highest_red_start,
        "red_end": highest_red_end,
    }


# ============================================================
# STREAK FREQUENCY ANALYSIS
# ============================================================

STREAK_LENGTHS = list(range(7, 16))


def calculate_streak_frequencies(candles):
    """
    Count how many independent GREEN and RED streaks reached at
    least 7, 8, 9, ... 15 consecutive candles.

    A single uninterrupted streak is counted once for every
    threshold it reaches.

    Example:
        A 10-RED-candle streak contributes:
            RED 7+  = 1
            RED 8+  = 1
            RED 9+  = 1
            RED 10+ = 1

        It does NOT count as multiple separate 7-candle events.

    DOJI candles terminate the current streak.
    """

    green_frequency = {length: 0 for length in STREAK_LENGTHS}
    red_frequency = {length: 0 for length in STREAK_LENGTHS}

    current_color = None
    current_length = 0

    def record_streak(color, length):
        if color == "GREEN":
            frequency = green_frequency
        elif color == "RED":
            frequency = red_frequency
        else:
            return

        for threshold in STREAK_LENGTHS:
            if length >= threshold:
                frequency[threshold] += 1

    for candle in candles:
        color = candle_color(candle)

        if color == current_color:
            current_length += 1
        else:
            # Finalize the previous uninterrupted streak.
            record_streak(current_color, current_length)

            current_color = color
            current_length = 1

    # Finalize the final streak in the dataset.
    record_streak(current_color, current_length)

    return {
        "green_frequency": green_frequency,
        "red_frequency": red_frequency,
    }


# ============================================================
# DISPLAY STREAK FREQUENCY TABLES
# ============================================================

def display_streak_frequency_table(results, color):
    """
    Display the frequency of GREEN or RED streaks reaching
    at least 7 through 15 consecutive candles.
    """

    color = color.upper()

    if color not in ("GREEN", "RED"):
        raise ValueError("color must be GREEN or RED")

    frequency_key = (
        "green_frequency"
        if color == "GREEN"
        else "red_frequency"
    )

    print("")
    print("=" * 125)
    print(
        f"{color} STREAK FREQUENCY TABLE "
        f"(AT LEAST N CONSECUTIVE CANDLES)"
    )
    print("=" * 125)

    print(
        f"{'PAIR':<15}"
        f"{'7+':>8}"
        f"{'8+':>8}"
        f"{'9+':>8}"
        f"{'10+':>8}"
        f"{'11+':>8}"
        f"{'12+':>8}"
        f"{'13+':>8}"
        f"{'14+':>8}"
        f"{'15+':>8}"
    )

    print("-" * 87)

    for result in results:
        frequency = result.get(
            frequency_key,
            {},
        )

        print(
            f"{result['pair']:<15}"
            f"{frequency.get(7, 0):>8}"
            f"{frequency.get(8, 0):>8}"
            f"{frequency.get(9, 0):>8}"
            f"{frequency.get(10, 0):>8}"
            f"{frequency.get(11, 0):>8}"
            f"{frequency.get(12, 0):>8}"
            f"{frequency.get(13, 0):>8}"
            f"{frequency.get(14, 0):>8}"
            f"{frequency.get(15, 0):>8}"
        )

    print("-" * 87)

    print("")
    print(
        f"Each {color} streak is counted once for every "
        "threshold it reaches."
    )
    print(
        "Example: one 10-candle streak contributes 1 event "
        "to 7+, 8+, 9+ and 10+."
    )
    print("")


# ============================================================
# FORMAT LOCAL TIME
# ============================================================

def format_report_time(timestamp):

    if timestamp is None:
        return "-"

    return (
        datetime.fromtimestamp(
            timestamp,
            tz=timezone.utc,
        )
        .astimezone(LOCAL_TIMEZONE)
        .strftime("%H:%M:%S")
    )



# ============================================================
# SQLITE DATABASE
# ============================================================

def get_database_connection():
    """Open the local SQLite database with useful safety settings."""
    connection = sqlite3.connect(str(DATABASE_FILE))
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def initialize_database():
    """
    Create the reporting schema if it does not already exist.

    Tables:
        report_runs          One row per generated report.
        pair_reports         One row per pair in a report.
        streak_frequencies  GREEN/RED 7+ through 15+ statistics.
        candle_data         The raw 1-minute candles used by the report.
    """
    with get_database_connection() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS report_runs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date         TEXT NOT NULL,
                market              TEXT NOT NULL,
                contract_type      TEXT NOT NULL,
                timeframe_seconds  INTEGER NOT NULL,
                timezone_name       TEXT NOT NULL,
                generated_at_utc   TEXT NOT NULL,
                expected_candles    INTEGER NOT NULL,
                database_version    INTEGER NOT NULL DEFAULT 1,
                UNIQUE (
                    report_date,
                    market,
                    contract_type,
                    timeframe_seconds
                )
            );

            CREATE TABLE IF NOT EXISTS pair_reports (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                report_run_id       INTEGER NOT NULL,
                pair                TEXT NOT NULL,
                candle_count        INTEGER NOT NULL DEFAULT 0,
                highest_green       INTEGER NOT NULL DEFAULT 0,
                green_start_utc     TEXT,
                green_end_utc       TEXT,
                highest_red         INTEGER NOT NULL DEFAULT 0,
                red_start_utc       TEXT,
                red_end_utc         TEXT,
                FOREIGN KEY (report_run_id)
                    REFERENCES report_runs(id)
                    ON DELETE CASCADE,
                UNIQUE (report_run_id, pair)
            );

            CREATE TABLE IF NOT EXISTS streak_frequencies (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_report_id      INTEGER NOT NULL,
                color               TEXT NOT NULL
                                    CHECK (color IN ('GREEN', 'RED')),
                threshold           INTEGER NOT NULL
                                    CHECK (threshold BETWEEN 7 AND 15),
                frequency           INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (pair_report_id)
                    REFERENCES pair_reports(id)
                    ON DELETE CASCADE,
                UNIQUE (
                    pair_report_id,
                    color,
                    threshold
                )
            );

            CREATE TABLE IF NOT EXISTS candle_data (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                pair_report_id      INTEGER NOT NULL,
                candle_timestamp_utc INTEGER NOT NULL,
                candle_time_utc     TEXT NOT NULL,
                candle_time_local   TEXT NOT NULL,
                open_price          REAL NOT NULL,
                close_price         REAL NOT NULL,
                high_price           REAL,
                low_price            REAL,
                candle_to_utc       TEXT,
                candle_to_local     TEXT,
                color               TEXT NOT NULL
                                    CHECK (color IN ('GREEN', 'RED', 'DOJI')),
                FOREIGN KEY (pair_report_id)
                    REFERENCES pair_reports(id)
                    ON DELETE CASCADE,
                UNIQUE (
                    pair_report_id,
                    candle_timestamp_utc
                )
            );

            CREATE INDEX IF NOT EXISTS idx_report_runs_date
                ON report_runs(report_date);

            CREATE INDEX IF NOT EXISTS idx_pair_reports_pair
                ON pair_reports(pair);

            CREATE INDEX IF NOT EXISTS idx_streak_frequency_lookup
                ON streak_frequencies(pair_report_id, color, threshold);

            CREATE INDEX IF NOT EXISTS idx_candle_data_lookup
                ON candle_data(pair_report_id, candle_timestamp_utc);

            CREATE INDEX IF NOT EXISTS idx_candle_data_color
                ON candle_data(pair_report_id, color);
            """
        )

    logger.info("SQLite database ready: %s", DATABASE_FILE)


def timestamp_to_utc_iso(timestamp):
    if timestamp is None:
        return None

    return datetime.fromtimestamp(
        float(timestamp),
        tz=timezone.utc,
    ).isoformat(timespec="seconds")


def timestamp_to_local_iso(timestamp):
    if timestamp is None:
        return None

    return (
        datetime.fromtimestamp(
            float(timestamp),
            tz=timezone.utc,
        )
        .astimezone(LOCAL_TIMEZONE)
        .isoformat(timespec="seconds")
    )


def save_report_to_database(results, candles_by_pair):
    """
    Save the displayed report into SQLite.

    The write happens only after all report tables have been displayed.

    If the same report date is run again, the previous copy for that
    date/market/timeframe is replaced as one transaction, preventing
    duplicate report rows.
    """
    initialize_database()

    generated_at_utc = datetime.now(
        timezone.utc
    ).isoformat(timespec="seconds")

    connection = get_database_connection()

    try:
        with connection:
            connection.execute(
                """
                DELETE FROM report_runs
                WHERE report_date = ?
                  AND market = ?
                  AND contract_type = ?
                  AND timeframe_seconds = ?
                """,
                (
                    REPORT_DATE,
                    "REAL FOREX",
                    "BINARY",
                    TIMEFRAME,
                ),
            )

            cursor = connection.execute(
                """
                INSERT INTO report_runs (
                    report_date,
                    market,
                    contract_type,
                    timeframe_seconds,
                    timezone_name,
                    generated_at_utc,
                    expected_candles,
                    database_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    REPORT_DATE,
                    "REAL FOREX",
                    "BINARY",
                    TIMEFRAME,
                    "Africa/Lagos / UTC+1",
                    generated_at_utc,
                    EXPECTED_CANDLES_PER_DAY,
                    DATABASE_SCHEMA_VERSION,
                ),
            )

            report_run_id = cursor.lastrowid

            for result in results:
                pair = result["pair"]

                pair_cursor = connection.execute(
                    """
                    INSERT INTO pair_reports (
                        report_run_id,
                        pair,
                        candle_count,
                        highest_green,
                        green_start_utc,
                        green_end_utc,
                        highest_red,
                        red_start_utc,
                        red_end_utc
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        report_run_id,
                        pair,
                        result.get("candles", 0),
                        result.get("highest_green", 0),
                        timestamp_to_utc_iso(result.get("green_start")),
                        timestamp_to_utc_iso(result.get("green_end")),
                        result.get("highest_red", 0),
                        timestamp_to_utc_iso(result.get("red_start")),
                        timestamp_to_utc_iso(result.get("red_end")),
                    ),
                )

                pair_report_id = pair_cursor.lastrowid

                for color, frequency_key in (
                    ("GREEN", "green_frequency"),
                    ("RED", "red_frequency"),
                ):
                    frequency = result.get(frequency_key, {})

                    for threshold in STREAK_LENGTHS:
                        connection.execute(
                            """
                            INSERT INTO streak_frequencies (
                                pair_report_id,
                                color,
                                threshold,
                                frequency
                            )
                            VALUES (?, ?, ?, ?)
                            """,
                            (
                                pair_report_id,
                                color,
                                threshold,
                                frequency.get(threshold, 0),
                            ),
                        )

                candle_rows = []

                for candle in candles_by_pair.get(pair, []):
                    try:
                        candle_from = int(float(candle["from"]))
                        candle_to = float(
                            candle.get(
                                "to",
                                candle_from + TIMEFRAME,
                            )
                        )

                        open_price = float(candle["open"])
                        close_price = float(candle["close"])

                        high_price = (
                            float(candle["max"])
                            if candle.get("max") is not None
                            else None
                        )

                        low_price = (
                            float(candle["min"])
                            if candle.get("min") is not None
                            else None
                        )

                        candle_rows.append(
                            (
                                pair_report_id,
                                candle_from,
                                timestamp_to_utc_iso(candle_from),
                                timestamp_to_local_iso(candle_from),
                                open_price,
                                close_price,
                                high_price,
                                low_price,
                                timestamp_to_utc_iso(candle_to),
                                timestamp_to_local_iso(candle_to),
                                candle_color(candle),
                            )
                        )

                    except (KeyError, TypeError, ValueError):
                        continue

                connection.executemany(
                    """
                    INSERT INTO candle_data (
                        pair_report_id,
                        candle_timestamp_utc,
                        candle_time_utc,
                        candle_time_local,
                        open_price,
                        close_price,
                        high_price,
                        low_price,
                        candle_to_utc,
                        candle_to_local,
                        color
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    candle_rows,
                )

                logger.info(
                    "Saved %-12s | pair ID: %d | candles: %d | "
                    "frequency rows: %d",
                    pair,
                    pair_report_id,
                    len(candle_rows),
                    len(STREAK_LENGTHS) * 2,
                )

        logger.info("SQLite report saved successfully: %s", DATABASE_FILE)
        return report_run_id

    except Exception:
        connection.rollback()
        logger.exception("SQLite database write failed.")
        raise

    finally:
        connection.close()


def display_database_summary(report_run_id):
    """Display a concise confirmation of what was persisted."""
    try:
        with get_database_connection() as connection:
            report = connection.execute(
                """
                SELECT
                    report_date,
                    market,
                    contract_type,
                    timeframe_seconds,
                    generated_at_utc
                FROM report_runs
                WHERE id = ?
                """,
                (report_run_id,),
            ).fetchone()

            pair_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM pair_reports
                WHERE report_run_id = ?
                """,
                (report_run_id,),
            ).fetchone()[0]

            candle_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM candle_data
                WHERE pair_report_id IN (
                    SELECT id
                    FROM pair_reports
                    WHERE report_run_id = ?
                )
                """,
                (report_run_id,),
            ).fetchone()[0]

            frequency_count = connection.execute(
                """
                SELECT COUNT(*)
                FROM streak_frequencies
                WHERE pair_report_id IN (
                    SELECT id
                    FROM pair_reports
                    WHERE report_run_id = ?
                )
                """,
                (report_run_id,),
            ).fetchone()[0]

        print("")
        print("=" * 90)
        print("SQLITE DATABASE PERSISTENCE")
        print("=" * 90)
        print(f"Database:       {DATABASE_FILE}")
        print(f"Report ID:      {report_run_id}")

        if report:
            print(
                f"Report:         {report[0]} | {report[1]} | "
                f"{report[2]} | {report[3]} seconds"
            )

        print(f"Pairs saved:    {pair_count}")
        print(f"Candles saved:  {candle_count}")
        print(f"Frequency rows: {frequency_count}")
        print("=" * 90)

    except Exception as exc:
        logger.exception(
            "Could not display SQLite persistence summary: %s",
            exc,
        )


# ============================================================
# DISPLAY MAIN REPORT
# ============================================================

def display_report(results):

    print("")
    print("")
    print("=" * 100)

    print(
        "FIREFLY AI - IQ OPTION REAL FOREX STREAK REPORT"
    )

    print("=" * 100)

    print(
        f"DATE: {REPORT_DATE}"
    )

    print(
        "MARKET: REAL FOREX"
    )

    print(
        "UNDERLYING FEED: IQ OPTION"
    )

    print(
        "CONTRACT EXAMPLE: USDJPY (Binary)"
    )

    print(
        "TIMEFRAME: 1 MINUTE"
    )

    print(
        "TIMEZONE: NIGERIA / UTC+1"
    )

    print("=" * 100)
    print("")

    print(
        f"{'PAIR':<15}"
        f"{'CANDLES':>12}"
        f"{'MAX GREEN':>15}"
        f"{'MAX RED':>15}"
    )

    print("-" * 57)

    for result in results:

        print(
            f"{result['pair']:<15}"
            f"{result['candles']:>12}"
            f"{result['highest_green']:>15}"
            f"{result['highest_red']:>15}"
        )

    print("-" * 57)

    print("")

    print(
        "GREEN = Close > Open"
    )

    print(
        "RED   = Close < Open"
    )

    print(
        "DOJI  = Close = Open and breaks the streak"
    )

    print("")


# ============================================================
# DISPLAY DETAILED STREAKS
# ============================================================

def display_detailed_streaks(results):

    print("")
    print("=" * 105)

    print(
        "HIGHEST REAL FOREX STREAK DETAILS"
    )

    print("=" * 105)

    print(
        f"{'PAIR':<15}"
        f"{'GREEN':>10}"
        f"{'GREEN TIME':>25}"
        f"{'RED':>10}"
        f"{'RED TIME':>25}"
    )

    print("-" * 105)

    for result in results:

        # ----------------------------------------------------
        # GREEN TIME
        # ----------------------------------------------------

        green_time = "-"

        if result["green_start"] is not None:

            green_time = (
                f"{format_report_time(result['green_start'])} - "
                f"{format_report_time(result['green_end'])}"
            )

        # ----------------------------------------------------
        # RED TIME
        # ----------------------------------------------------

        red_time = "-"

        if result["red_start"] is not None:

            red_time = (
                f"{format_report_time(result['red_start'])} - "
                f"{format_report_time(result['red_end'])}"
            )

        print(
            f"{result['pair']:<15}"
            f"{result['highest_green']:>10}"
            f"{green_time:>25}"
            f"{result['highest_red']:>10}"
            f"{red_time:>25}"
        )

    print("-" * 105)
    print("")


# ============================================================
# DISPLAY USDJPY DETAILED RESULT
# ============================================================

def display_usdjpy_result(result):

    print("")
    print("=" * 80)

    print(
        "USD/JPY REAL FOREX MARKET ANALYSIS"
    )

    print("=" * 80)

    print(
        f"Date: {REPORT_DATE}"
    )

    print(
        "Source: IQ Option direct candle feed"
    )

    print(
        "Asset: USDJPY"
    )

    print(
        "Contract: Binary"
    )

    print(
        "Timeframe: 1 minute"
    )

    print(
        f"Candles analyzed: {result['candles']}"
    )

    print("")

    print(
        f"Maximum GREEN streak: "
        f"{result['highest_green']}"
    )

    if result["green_start"] is not None:

        print(
            "GREEN streak time: "
            f"{format_report_time(result['green_start'])}"
            f" -> "
            f"{format_report_time(result['green_end'])}"
        )

    print("")

    print(
        f"Maximum RED streak: "
        f"{result['highest_red']}"
    )

    if result["red_start"] is not None:

        print(
            "RED streak time: "
            f"{format_report_time(result['red_start'])}"
            f" -> "
            f"{format_report_time(result['red_end'])}"
        )

    print("=" * 80)


# ============================================================
# GENERATE REPORT
# ============================================================

def generate_report():

    logger.info("")
    logger.info("=" * 80)

    logger.info(
        "FIREFLY AI - IQ OPTION REAL FOREX REPORT"
    )

    logger.info("=" * 80)

    logger.info(
        "Report date: %s",
        REPORT_DATE,
    )

    logger.info(
        "Market: REAL FOREX"
    )

    logger.info(
        "Timeframe: 1 minute"
    )

    logger.info(
        "Timezone: Nigeria UTC+1"
    )

    logger.info("=" * 80)

    # ========================================================
    # CONNECT
    # ========================================================

    if not connect_iq_option():

        logger.error(
            "Unable to connect to IQ Option."
        )

        return

    # ========================================================
    # VERIFY REAL FOREX ASSETS
    # ========================================================

    if not check_real_forex_assets():

        logger.error(
            "Could not obtain all real forex asset mappings."
        )

        return

    # ========================================================
    # PROCESS PAIRS
    # ========================================================

    results = []
    candles_by_pair = {}

    for pair in FOREX_PAIRS:

        candles = get_historical_candles(
            pair
        )

        # Keep the exact dataset used by the report so it can be
        # persisted after the report has been displayed.
        candles_by_pair[pair] = candles

        if not candles:

            logger.error(
                "No historical data available for %s",
                pair,
            )

            results.append(
                {
                    "pair": pair,
                    "candles": 0,

                    "highest_green": 0,
                    "green_start": None,
                    "green_end": None,

                    "highest_red": 0,
                    "red_start": None,
                    "red_end": None,
                    "green_frequency": {
                        length: 0 for length in STREAK_LENGTHS
                    },
                    "red_frequency": {
                        length: 0 for length in STREAK_LENGTHS
                    },
                }
            )

            continue

        # ----------------------------------------------------
        # Calculate highest streaks
        # ----------------------------------------------------

        details = find_highest_streak_details(
            candles
        )

        # ----------------------------------------------------
        # Calculate 7-15 candle streak frequencies
        # ----------------------------------------------------

        frequencies = calculate_streak_frequencies(
            candles
        )

        result = {
            "pair": pair,

            "candles": len(candles),

            "highest_green":
                details["highest_green"],

            "green_start":
                details["green_start"],

            "green_end":
                details["green_end"],

            "highest_red":
                details["highest_red"],

            "red_start":
                details["red_start"],

            "red_end":
                details["red_end"],

            "green_frequency":
                frequencies["green_frequency"],

            "red_frequency":
                frequencies["red_frequency"],
        }

        results.append(result)

        logger.info(
            "%-12s | Candles: %4d | "
            "Max GREEN: %2d | "
            "Max RED: %2d",
            pair,
            len(candles),
            details["highest_green"],
            details["highest_red"],
        )

        # ----------------------------------------------------
        # Special USDJPY output
        # ----------------------------------------------------

        if pair == "USDJPY":

            display_usdjpy_result(
                result
            )

    # ========================================================
    # FINAL REPORT
    # ========================================================

    display_report(results)

    display_detailed_streaks(results)

    # --------------------------------------------------------
    # 7-15 CANDLE STREAK FREQUENCY TABLES
    # --------------------------------------------------------

    display_streak_frequency_table(
        results,
        "GREEN"
    )

    display_streak_frequency_table(
        results,
        "RED"
    )

    # ========================================================
    # PERSIST REPORT AFTER DISPLAY
    # ========================================================
    # All report tables are printed first. Only then is the
    # complete report dataset written to SQLite.

    report_run_id = save_report_to_database(
        results,
        candles_by_pair,
    )

    display_database_summary(
        report_run_id
    )

    logger.info("")
    logger.info(
        "Historical real forex report completed."
    )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        generate_report()

    except KeyboardInterrupt:

        print(
            "\nReport generation stopped by user."
        )

    except Exception as exc:

        logger.exception(
            "Fatal error: %s",
            exc,
        )

