import os
import sys
import time
import logging
import sqlite3
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

TIMEFRAME = 60

# We need 1,440 one-minute candles for one complete day.
CANDLES_PER_DAY = 24 * 60

# Number of candles requested in each API call.
# Using 500 keeps the requests comfortably below common
# API limits and allows us to retrieve the entire day in
# multiple batches.
CANDLE_BATCH_SIZE = 500

# Streak lengths for the frequency analysis.
# A streak is counted once at every threshold it reaches.
# Example: one run of 10 RED candles contributes:
# 1 occurrence to 7+, 8+, 9+ and 10+.
STREAK_LENGTHS = list(range(7, 16))

# Date we are analyzing.
# DATE FORMAT: YYYY-MM-DD
REPORT_DATE = "2026-09-21"

# Nigeria / West Africa timezone = UTC+1.
# This makes 00:00:00 and 23:59:00 correspond to
# the user's local Nigerian time.
LOCAL_TIMEZONE = timezone(timedelta(hours=1))

# SQLite database for OTC reports.
# Kept separate from the real-forex report database.
DATABASE_FILE = "firefly_otc_reports.db"


# ============================================================
# OTC PAIRS
# ============================================================

OTC_PAIRS = [
    "USDJPY-OTC",
    "AUDUSD-OTC",
    "EURJPY-OTC",
    "AUDJPY-OTC",
    "GBPJPY-OTC",
    "GBPAUD-OTC",
    "GBPCAD-OTC",
    "CADJPY-OTC",
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

logger = logging.getLogger("OTC-REPORT")


# ============================================================
# GLOBAL
# ============================================================

API = None


# ============================================================
# CONNECTION
# ============================================================

def connect_iq_option():
    """
    Connect to IQ Option.
    """

    global API

    logger.info("=" * 70)
    logger.info("CONNECTING TO IQ OPTION")
    logger.info("=" * 70)

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

        logger.info(
            "IQ Option connection successful."
        )

        # Use PRACTICE account.
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
# REFRESH OTC ASSET MAPPINGS
# ============================================================

def refresh_otc_asset_mappings():
    """
    Obtain the IQ Option asset IDs for the eight OTC pairs.

    First checks whether the IDs are already available in
    stable_api.OP_code.ACTIVES.

    Only requests api_option_init_all when required.
    """

    logger.info("")
    logger.info("=" * 70)
    logger.info("REFRESHING OTC ASSET MAPPINGS")
    logger.info("=" * 70)

    # --------------------------------------------------------
    # FIRST: Check existing mappings
    # --------------------------------------------------------

    missing = [
        pair
        for pair in OTC_PAIRS
        if stable_api.OP_code.ACTIVES.get(pair) is None
    ]

    if not missing:

        logger.info(
            "All OTC asset mappings are already available."
        )

        logger.info("")
        logger.info("CURRENT OTC ASSET MAPPINGS")
        logger.info("-" * 70)

        for pair in OTC_PAIRS:

            active_id = stable_api.OP_code.ACTIVES.get(pair)

            logger.info(
                "READY: %-15s -> %s",
                pair,
                active_id,
            )

        logger.info("-" * 70)

        return True

    logger.info(
        "Missing OTC mappings: %s",
        ", ".join(missing),
    )

    # --------------------------------------------------------
    # SECOND: Request fresh mappings
    # --------------------------------------------------------

    try:

        API.api.api_option_init_all_result = None

        start_time = time.time()

        logger.info(
            "Requesting binary/turbo initialization data..."
        )

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

        if not result.get("isSuccessful"):

            logger.error(
                "IQ Option returned unsuccessful asset initialization."
            )

            return False

        result_data = result.get(
            "result",
            {},
        )

        # ----------------------------------------------------
        # Extract binary + turbo assets
        # ----------------------------------------------------

        for market_type in (
            "binary",
            "turbo",
        ):

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

                    if "." in raw_name:

                        asset_name = raw_name.split(
                            ".",
                            1,
                        )[1]

                    else:

                        asset_name = raw_name

                    if asset_name in OTC_PAIRS:

                        numeric_id = int(active_id)

                        stable_api.OP_code.ACTIVES[
                            asset_name
                        ] = numeric_id

                except Exception:

                    continue

        # ----------------------------------------------------
        # Verify mappings
        # ----------------------------------------------------

        logger.info("")
        logger.info("CURRENT OTC ASSET MAPPINGS")
        logger.info("-" * 70)

        all_found = True

        for pair in OTC_PAIRS:

            active_id = stable_api.OP_code.ACTIVES.get(
                pair
            )

            if active_id is not None:

                logger.info(
                    "READY: %-15s -> %s",
                    pair,
                    active_id,
                )

            else:

                logger.error(
                    "MISSING: %-15s -> NOT FOUND",
                    pair,
                )

                all_found = False

        logger.info("-" * 70)

        return all_found

    except Exception as exc:

        logger.exception(
            "OTC asset refresh failed: %s",
            exc,
        )

        return False

# ============================================================
# DATE RANGE
# ============================================================

def get_report_range():
    """
    Convert the requested Nigerian local date into Unix
    timestamps.

    Report period:

        REPORT_DATE 00:00:00
        through
        REPORT_DATE 23:59:00

    The end boundary is the start of the next day.
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

    local_end = local_start + timedelta(
        days=1
    )

    start_timestamp = int(
        local_start.timestamp()
    )

    end_timestamp = int(
        local_end.timestamp()
    )

    return (
        start_timestamp,
        end_timestamp,
        local_start,
        local_end,
    )


# ============================================================
# FETCH HISTORICAL CANDLES
# ============================================================

def get_historical_candles(pair):
    """
    Retrieve all one-minute candles for the requested date.

    Because one day contains 1,440 one-minute candles, the
    candles are retrieved in multiple batches.

    Returns:
        list of candles covering the requested date.
    """

    if stable_api.OP_code.ACTIVES.get(pair) is None:

        logger.error(
            "%s has no active ID.",
            pair,
        )

        return []

    (
        start_timestamp,
        end_timestamp,
        local_start,
        local_end,
    ) = get_report_range()

    logger.info("")
    logger.info(
        "Fetching %s",
        pair,
    )

    logger.info(
        "Period: %s to %s",
        local_start.strftime("%Y-%m-%d %H:%M:%S"),
        (
            local_end - timedelta(seconds=60)
        ).strftime("%Y-%m-%d %H:%M:%S"),
    )

    all_candles = []

    # Start from the end of the requested period.
    cursor = end_timestamp

    batch_number = 0

    while cursor > start_timestamp:

        batch_number += 1

        try:

            logger.info(
                "  Batch %d: requesting up to %d candles...",
                batch_number,
                CANDLE_BATCH_SIZE,
            )

            candles = API.get_candles(
                pair,
                TIMEFRAME,
                CANDLE_BATCH_SIZE,
                cursor,
            )

            if not candles:

                logger.warning(
                    "  No candles returned."
                )

                break

            logger.info(
                "  Received %d candles.",
                len(candles),
            )

            # ------------------------------------------------
            # Add only candles belonging to requested period.
            # ------------------------------------------------

            oldest_timestamp = None

            for candle in candles:

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

                if (
                    candle_from >= start_timestamp
                    and candle_from < end_timestamp
                    and candle_to <= end_timestamp
                ):

                    all_candles.append(
                        candle
                    )

                if oldest_timestamp is None:

                    oldest_timestamp = candle_from

                else:

                    oldest_timestamp = min(
                        oldest_timestamp,
                        candle_from,
                    )

            # ------------------------------------------------
            # Move cursor backwards.
            # ------------------------------------------------

            if oldest_timestamp is None:

                break

            next_cursor = int(
                oldest_timestamp
            ) - 1

            # Protect against cursor not moving.
            if next_cursor >= cursor:

                logger.warning(
                    "Cursor did not move backwards. Stopping."
                )

                break

            cursor = next_cursor

            # Safety limit.
            if batch_number > 10:

                logger.warning(
                    "Too many batches. Stopping for safety."
                )

                break

        except Exception as exc:

            logger.error(
                "%s batch error: %s",
                pair,
                exc,
            )

            break

    # --------------------------------------------------------
    # Remove duplicates.
    # --------------------------------------------------------

    unique_candles = {}

    for candle in all_candles:

        candle_from = int(
            float(candle["from"])
        )

        unique_candles[candle_from] = candle

    # --------------------------------------------------------
    # Sort chronologically.
    # --------------------------------------------------------

    candles = sorted(
        unique_candles.values(),
        key=lambda x: float(x["from"]),
    )

    logger.info(
        "  Total unique candles collected: %d",
        len(candles),
    )

    return candles


# ============================================================
# CANDLE COLOR
# ============================================================

def candle_color(candle):
    """
    Determine whether a candle is GREEN, RED or DOJI.
    """

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
# STREAK ANALYSIS
# ============================================================

def calculate_max_streaks(candles):
    """
    Calculate the maximum consecutive GREEN and RED streaks
    across the entire requested period.

    A DOJI breaks both streaks.
    """

    highest_green = 0
    highest_red = 0

    current_green = 0
    current_red = 0

    for candle in candles:

        color = candle_color(
            candle
        )

        if color == "GREEN":

            current_green += 1
            current_red = 0

            highest_green = max(
                highest_green,
                current_green,
            )

        elif color == "RED":

            current_red += 1
            current_green = 0

            highest_red = max(
                highest_red,
                current_red,
            )

        else:

            # DOJI breaks both streaks.
            current_green = 0
            current_red = 0

    return (
        highest_green,
        highest_red,
    )


# ============================================================
# STREAK FREQUENCY ANALYSIS
# ============================================================

def calculate_streak_frequency(candles):
    """
    Count how many distinct GREEN and RED streak runs reached
    each threshold from 7 through 15 candles.

    Important counting rule:
        A single uninterrupted run is counted ONCE for every
        threshold it reaches, rather than counting overlapping
        windows inside the same run.

    Example:
        A run of 10 RED candles contributes:
            7+  -> 1
            8+  -> 1
            9+  -> 1
            10+ -> 1
            11+ -> 0

        This makes the table represent the number of streak EVENTS
        that reached each length, which is more useful for evaluating
        a 7-candle reversal strategy.

    A DOJI ends the current streak and does not count toward either
    color.

    Returns:
        {
            "green": {7: count, 8: count, ..., 15: count},
            "red":   {7: count, 8: count, ..., 15: count}
        }
    """

    green_frequency = {length: 0 for length in STREAK_LENGTHS}
    red_frequency = {length: 0 for length in STREAK_LENGTHS}

    current_color = None
    current_length = 0

    def record_completed_streak(color, length):
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
            continue

        # The previous run has ended because the color changed or
        # a DOJI appeared. Record it before starting the new run.
        if current_color in ("GREEN", "RED"):
            record_completed_streak(current_color, current_length)

        if color in ("GREEN", "RED"):
            current_color = color
            current_length = 1
        else:
            current_color = None
            current_length = 0

    # Record the final run because there may be no following candle
    # to trigger the color-change logic.
    if current_color in ("GREEN", "RED"):
        record_completed_streak(current_color, current_length)

    return {
        "green": green_frequency,
        "red": red_frequency,
    }


# ============================================================
# FIND STREAK DETAILS
# ============================================================

def find_highest_streak_details(candles):
    """
    Find the highest GREEN and RED streaks and their
    start/end times.

    Returns:

        green_count
        green_start
        green_end
        red_count
        red_start
        red_end
    """

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

        color = candle_color(
            candle
        )

        # ----------------------------------------------------
        # GREEN
        # ----------------------------------------------------

        if color == "GREEN":

            current_green += 1
            current_red = 0

            if current_green == 1:

                current_green_start = candle_from

            if current_green > highest_green:

                highest_green = current_green

                highest_green_start = (
                    current_green_start
                )

                highest_green_end = (
                    candle_to
                )

        # ----------------------------------------------------
        # RED
        # ----------------------------------------------------

        elif color == "RED":

            current_red += 1
            current_green = 0

            if current_red == 1:

                current_red_start = candle_from

            if current_red > highest_red:

                highest_red = current_red

                highest_red_start = (
                    current_red_start
                )

                highest_red_end = (
                    candle_to
                )

        # ----------------------------------------------------
        # DOJI
        # ----------------------------------------------------

        else:

            current_green = 0
            current_red = 0

    return (
        highest_green,
        highest_green_start,
        highest_green_end,
        highest_red,
        highest_red_start,
        highest_red_end,
    )


# ============================================================
# FORMAT LOCAL TIME
# ============================================================

def format_report_time(timestamp):
    """
    Convert Unix timestamp to Nigerian local time.
    """

    if timestamp is None:

        return "-"

    return datetime.fromtimestamp(
        timestamp,
        tz=LOCAL_TIMEZONE,
    ).strftime(
        "%H:%M"
    )


# ============================================================
# DISPLAY TABLE
# ============================================================

def display_report(results):
    """
    Display the final report in table format.
    """

    print("")
    print("")
    print("=" * 90)
    print(
        "FIREFLY AI - OTC CANDLESTICK STREAK REPORT"
    )
    print("=" * 90)

    print(
        f"DATE: {REPORT_DATE}"
    )

    print(
        "PERIOD: 00:00:00 - 23:59:00"
    )

    print(
        "TIMEFRAME: 1 MINUTE"
    )

    print(
        f"DATABASE: {DATABASE_FILE}"
    )

    print("=" * 90)

    # --------------------------------------------------------
    # Main requested table.
    # --------------------------------------------------------

    print("")
    print(
        f"{'CURRENCY PAIR':<20}"
        f"{'CANDLES':>10}"
        f"{'MAX GREEN':>15}"
        f"{'MAX RED':>15}"
    )

    print("-" * 60)

    for result in results:

        print(
            f"{result['pair']:<20}"
            f"{result['candles']:>10}"
            f"{result['highest_green']:>15}"
            f"{result['highest_red']:>15}"
        )

    print("-" * 60)

    print("")
    print(
        "MAXIMUM GREEN = highest number of consecutive GREEN candles"
    )

    print(
        "MAXIMUM RED   = highest number of consecutive RED candles"
    )

    print("")


# ============================================================
# DISPLAY DETAILED STREAK INFORMATION
# ============================================================

def display_detailed_streaks(results):
    """
    Display the times at which the maximum streaks occurred.

    This is additional information to make the report useful
    for analyzing the 7-candle reversal strategy.
    """

    print("")
    print("=" * 90)
    print("HIGHEST STREAK DETAILS")
    print("=" * 90)

    print(
        f"{'PAIR':<18}"
        f"{'GREEN STREAK':>15}"
        f"{'GREEN TIME':>22}"
        f"{'RED STREAK':>15}"
        f"{'RED TIME':>22}"
    )

    print("-" * 95)

    for result in results:

        green_time = "-"

        if result["green_start"] is not None:

            green_time = (
                f"{format_report_time(result['green_start'])}"
                f" - "
                f"{format_report_time(result['green_end'])}"
            )

        red_time = "-"

        if result["red_start"] is not None:

            red_time = (
                f"{format_report_time(result['red_start'])}"
                f" - "
                f"{format_report_time(result['red_end'])}"
            )

        print(
            f"{result['pair']:<18}"
            f"{result['highest_green']:>15}"
            f"{green_time:>22}"
            f"{result['highest_red']:>15}"
            f"{red_time:>22}"
        )

    print("-" * 95)
    print("")


# ============================================================
# DISPLAY STREAK FREQUENCY TABLE
# ============================================================

def display_streak_frequency(results):
    """
    Display how many streak EVENTS reached each consecutive-candle
    threshold from 7 through 15 for every OTC pair.

    GREEN and RED are displayed separately so the report can show
    whether long bullish or bearish runs were more frequent.
    """

    print("")
    print("=" * 150)
    print("STREAK FREQUENCY TABLE - 7 TO 15 CONSECUTIVE CANDLES")
    print("=" * 150)
    print(
        "Each number represents the number of distinct streak runs "
        "that reached AT LEAST that many candles."
    )
    print(
        "Example: one 10-candle RED run counts once under 7+, 8+, 9+ "
        "and 10+, but not under 11+ through 15+."
    )
    print("")

    header = f"{'PAIR':<16}"
    for length in STREAK_LENGTHS:
        header += f"{length:>6}"

    print("GREEN STREAKS (AT LEAST N CONSECUTIVE GREEN CANDLES)")
    print("-" * len(header))
    print(header)
    print("-" * len(header))

    for result in results:
        frequency = result.get("green_frequency", {})
        row = f"{result['pair']:<16}"
        for length in STREAK_LENGTHS:
            row += f"{frequency.get(length, 0):>6}"
        print(row)

    print("-" * len(header))
    print("")

    print("RED STREAKS (AT LEAST N CONSECUTIVE RED CANDLES)")
    print("-" * len(header))
    print(header)
    print("-" * len(header))

    for result in results:
        frequency = result.get("red_frequency", {})
        row = f"{result['pair']:<16}"
        for length in STREAK_LENGTHS:
            row += f"{frequency.get(length, 0):>6}"
        print(row)

    print("-" * len(header))
    print("")


# ============================================================
# SQLITE DATABASE
# ============================================================

def initialize_database():
    """
    Create the separate OTC SQLite database and its tables if they
    do not already exist.

    Database file:
        firefly_otc_reports.db
    """
    connection = sqlite3.connect(DATABASE_FILE)

    try:
        cursor = connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS report_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL,
                timeframe_seconds INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                total_pairs INTEGER NOT NULL,
                status TEXT NOT NULL
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pair_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_run_id INTEGER NOT NULL,
                pair TEXT NOT NULL,
                candle_count INTEGER NOT NULL,
                highest_green INTEGER NOT NULL,
                highest_red INTEGER NOT NULL,
                green_start_timestamp INTEGER,
                green_end_timestamp INTEGER,
                red_start_timestamp INTEGER,
                red_end_timestamp INTEGER,
                FOREIGN KEY (report_run_id) REFERENCES report_runs(id),
                UNIQUE(report_run_id, pair)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS streak_frequencies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_run_id INTEGER NOT NULL,
                pair TEXT NOT NULL,
                color TEXT NOT NULL,
                streak_length INTEGER NOT NULL,
                occurrence_count INTEGER NOT NULL,
                FOREIGN KEY (report_run_id) REFERENCES report_runs(id),
                UNIQUE(
                    report_run_id,
                    pair,
                    color,
                    streak_length
                )
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS candle_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_run_id INTEGER NOT NULL,
                pair TEXT NOT NULL,
                candle_timestamp INTEGER NOT NULL,
                candle_end_timestamp INTEGER,
                open_price REAL NOT NULL,
                close_price REAL NOT NULL,
                high_price REAL,
                low_price REAL,
                volume REAL,
                candle_color TEXT NOT NULL,
                FOREIGN KEY (report_run_id) REFERENCES report_runs(id),
                UNIQUE(report_run_id, pair, candle_timestamp)
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_otc_pair_reports_run
            ON pair_reports(report_run_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_otc_streak_frequency_run
            ON streak_frequencies(report_run_id)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_otc_candle_data_pair_time
            ON candle_data(pair, candle_timestamp)
        """)

        connection.commit()

        logger.info(
            "OTC SQLite database ready: %s",
            DATABASE_FILE,
        )

    finally:
        connection.close()


def save_report_to_database(results):
    """
    Persist the completed OTC report into the separate SQLite database.

    Every execution creates a new report_runs record, so historical
    report dates/runs are preserved rather than overwritten.
    """
    initialize_database()

    connection = sqlite3.connect(DATABASE_FILE)

    try:
        cursor = connection.cursor()

        created_at = datetime.now(
            tz=LOCAL_TIMEZONE
        ).isoformat(timespec="seconds")

        cursor.execute("""
            INSERT INTO report_runs (
                report_date,
                timeframe_seconds,
                created_at,
                total_pairs,
                status
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            REPORT_DATE,
            TIMEFRAME,
            created_at,
            len(results),
            "COMPLETED",
        ))

        report_run_id = cursor.lastrowid

        for result in results:
            cursor.execute("""
                INSERT INTO pair_reports (
                    report_run_id,
                    pair,
                    candle_count,
                    highest_green,
                    highest_red,
                    green_start_timestamp,
                    green_end_timestamp,
                    red_start_timestamp,
                    red_end_timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                report_run_id,
                result["pair"],
                result["candles"],
                result["highest_green"],
                result["highest_red"],
                int(result["green_start"])
                if result["green_start"] is not None else None,
                int(result["green_end"])
                if result["green_end"] is not None else None,
                int(result["red_start"])
                if result["red_start"] is not None else None,
                int(result["red_end"])
                if result["red_end"] is not None else None,
            ))

            for color, frequency_key in (
                ("GREEN", "green_frequency"),
                ("RED", "red_frequency"),
            ):
                frequency = result.get(
                    frequency_key,
                    {}
                )

                for streak_length in STREAK_LENGTHS:
                    cursor.execute("""
                        INSERT INTO streak_frequencies (
                            report_run_id,
                            pair,
                            color,
                            streak_length,
                            occurrence_count
                        )
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        report_run_id,
                        result["pair"],
                        color,
                        streak_length,
                        frequency.get(streak_length, 0),
                    ))

        connection.commit()

        logger.info(
            "OTC report saved to SQLite. Run ID: %s | Database: %s",
            report_run_id,
            DATABASE_FILE,
        )

        return report_run_id

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


def save_candles_to_database(report_run_id, pair, candles):
    """
    Save the raw one-minute OTC candles used for the report.

    Existing candles for the same report run/pair/timestamp are ignored
    so duplicate API batches do not create duplicate database rows.
    """
    if not candles:
        return 0

    connection = sqlite3.connect(DATABASE_FILE)

    try:
        cursor = connection.cursor()

        rows = []

        for candle in candles:
            candle_from = int(float(candle["from"]))
            candle_to = int(
                float(
                    candle.get(
                        "to",
                        candle_from + TIMEFRAME,
                    )
                )
            )

            open_price = float(candle["open"])
            close_price = float(candle["close"])

            high_price = (
                float(candle["max"])
                if candle.get("max") is not None
                else float(candle.get("high", open_price))
            )

            low_price = (
                float(candle["min"])
                if candle.get("min") is not None
                else float(candle.get("low", open_price))
            )

            volume_value = candle.get("volume")

            volume = (
                float(volume_value)
                if volume_value is not None
                else None
            )

            rows.append((
                report_run_id,
                pair,
                candle_from,
                candle_to,
                open_price,
                close_price,
                high_price,
                low_price,
                volume,
                candle_color(candle),
            ))

        cursor.executemany("""
            INSERT OR IGNORE INTO candle_data (
                report_run_id,
                pair,
                candle_timestamp,
                candle_end_timestamp,
                open_price,
                close_price,
                high_price,
                low_price,
                volume,
                candle_color
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, rows)

        connection.commit()

        return cursor.rowcount

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


# ============================================================
# GENERATE REPORT
# ============================================================

def generate_report():

    global API

    logger.info("")
    logger.info("=" * 70)
    logger.info("FIREFLY AI HISTORICAL OTC REPORT")
    logger.info("=" * 70)

    logger.info(
        "Report date: %s",
        REPORT_DATE,
    )

    logger.info(
        "Timeframe: 1 minute",
    )

    logger.info(
        "Expected candles per pair: %d",
        CANDLES_PER_DAY,
    )

    logger.info("=" * 70)

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    if not connect_iq_option():

        logger.error(
            "Unable to connect to IQ Option."
        )

        return

    # --------------------------------------------------------
    # REFRESH ASSETS
    # --------------------------------------------------------

    if not refresh_otc_asset_mappings():

        logger.error(
            "Could not obtain OTC asset mappings."
        )

        return

    # --------------------------------------------------------
    # PROCESS ALL PAIRS
    # --------------------------------------------------------

    results = []

    for pair in OTC_PAIRS:

        candles = get_historical_candles(
            pair
        )

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
                    "highest_red": 0,
                    "green_start": None,
                    "green_end": None,
                    "red_start": None,
                    "red_end": None,
                    "green_frequency": {
                        length: 0 for length in STREAK_LENGTHS
                    },
                    "red_frequency": {
                        length: 0 for length in STREAK_LENGTHS
                    },
                    "candles_data": [],
                }
            )

            continue

        (
            highest_green,
            green_start,
            green_end,
            highest_red,
            red_start,
            red_end,
        ) = find_highest_streak_details(
            candles
        )

        streak_frequency = calculate_streak_frequency(candles)

        results.append(
            {
                "pair": pair,
                "candles": len(candles),
                "highest_green": highest_green,
                "highest_red": highest_red,
                "green_start": green_start,
                "green_end": green_end,
                "red_start": red_start,
                "red_end": red_end,
                "green_frequency": streak_frequency["green"],
                "red_frequency": streak_frequency["red"],
                "candles_data": candles,
            }
        )

        logger.info(
            "%-15s | Candles: %4d | "
            "Max GREEN: %2d | Max RED: %2d",
            pair,
            len(candles),
            highest_green,
            highest_red,
        )

    # --------------------------------------------------------
    # DISPLAY
    # --------------------------------------------------------

    display_report(
        results
    )

    display_detailed_streaks(
        results
    )

    display_streak_frequency(
        results
    )

    # --------------------------------------------------------
    # SAVE TO SEPARATE OTC SQLITE DATABASE
    # --------------------------------------------------------

    report_run_id = save_report_to_database(
        results
    )

    total_saved_candles = 0

    for result in results:
        saved_count = save_candles_to_database(
            report_run_id,
            result["pair"],
            result.get("candles_data", []),
        )

        total_saved_candles += saved_count

    logger.info(
        "Saved %d candle rows to %s.",
        total_saved_candles,
        DATABASE_FILE,
    )

    logger.info(
        "Historical OTC report completed."
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