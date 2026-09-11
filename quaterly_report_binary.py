import csv
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option
from iqoptionapi import stable_api


# =============================================================================
# FIREFLY AI - QUARTERLY REAL FOREX BINARY CANDLE REPORT
# =============================================================================
#
# PURPOSE
# -------
# Download and analyze 1-minute candles directly from IQ Option for the
# REAL FOREX pairs used by Binary Options.
#
# IMPORTANT:
#
# These are NOT OTC assets.
#
# Correct:
#       USDJPY
#       AUDUSD
#       EURJPY
#
# Incorrect for this report:
#       USDJPY-OTC
#       AUDUSD-OTC
#       EURJPY-OTC
#
# Binary is the trading contract type.
# The underlying asset is the real forex pair.
#
# DEFAULT REPORT:
#       Q2 2026
#
#       April 1, 2026
#       through
#       June 30, 2026
#
# Strategy:
#
#       7 consecutive GREEN candles -> SELL / LOWER
#       7 consecutive RED candles   -> BUY / HIGHER
#
# DOJI breaks a streak.
#
# No trades are placed by this program.
#
# =============================================================================


# =============================================================================
# CONFIGURATION
# =============================================================================

TIMEFRAME = 60                    # 1-minute candles

# IQ Option's candle API supports large batches. 1000 is used to reduce
# the number of requests required for a quarterly report.
CANDLE_BATCH_SIZE = 1000

STREAK_LENGTH_REQUIRED = 7

MAX_RETRIES = 5

RECONNECT_WAIT_SECONDS = 5

REQUEST_DELAY_SECONDS = 0.25

MAX_EMPTY_BATCHES = 3

# Display/report timezone.
# Nigeria / West Africa Time = UTC+1
WAT = timezone(timedelta(hours=1))


# =============================================================================
# QUARTER CONFIGURATION
# =============================================================================
#
# Current date:
# September 8, 2026
#
# The last COMPLETED calendar quarter is:
#
# Q2 2026
# April 1, 2026 -> June 30, 2026
#
# Change these two values if you want another quarter.
#
# Examples:
#
# REPORT_YEAR = 2026
# REPORT_QUARTER = 1
#
# Q1 = January - March
# Q2 = April - June
# Q3 = July - September
# Q4 = October - December
#
# =============================================================================

REPORT_YEAR = 2026

# Last completed quarter as of September 8, 2026.
REPORT_QUARTER = 2


# =============================================================================
# EIGHT FAVORITE REAL FOREX PAIRS
# =============================================================================
#
# These are REAL forex symbols.
#
# Do NOT append "-OTC".
#
# =============================================================================

CURRENCY_PAIRS = [
    ("USD/JPY", "USDJPY"),
    ("AUD/USD", "AUDUSD"),
    ("EUR/JPY", "EURJPY"),
    ("AUD/JPY", "AUDJPY"),
    ("GBP/JPY", "GBPJPY"),
    ("GBP/AUD", "GBPAUD"),
    ("GBP/CAD", "GBPCAD"),
    ("CAD/JPY", "CADJPY"),
]


# =============================================================================
# DIRECTORIES
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent

DATA_DIR = BASE_DIR / "data"

REPORT_DIR = BASE_DIR / "reports"

DATA_DIR.mkdir(exist_ok=True)

REPORT_DIR.mkdir(exist_ok=True)


# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("firefly")


# =============================================================================
# ENVIRONMENT
# =============================================================================

load_dotenv()

EMAIL = os.getenv("IQ_EMAIL")

PASSWORD = os.getenv("IQ_PASSWORD")


# =============================================================================
# QUARTER DATE CALCULATION
# =============================================================================

def get_quarter_dates(year, quarter):
    """
    Return the start and exclusive-end datetime for a calendar quarter.

    Q1 = Jan 1  -> Apr 1
    Q2 = Apr 1  -> Jul 1
    Q3 = Jul 1  -> Oct 1
    Q4 = Oct 1  -> Jan 1 next year
    """

    if quarter not in (1, 2, 3, 4):
        raise ValueError(
            "REPORT_QUARTER must be 1, 2, 3, or 4."
        )

    start_month = ((quarter - 1) * 3) + 1

    start_date = datetime(
        year,
        start_month,
        1,
        0,
        0,
        0,
        tzinfo=WAT,
    )

    if quarter == 4:
        end_date = datetime(
            year + 1,
            1,
            1,
            0,
            0,
            0,
            tzinfo=WAT,
        )
    else:
        end_date = datetime(
            year,
            start_month + 3,
            1,
            0,
            0,
            0,
            tzinfo=WAT,
        )

    return start_date, end_date


START_DATE, END_DATE_EXCLUSIVE = get_quarter_dates(
    REPORT_YEAR,
    REPORT_QUARTER,
)


# =============================================================================
# QUARTER NAME
# =============================================================================

def quarter_name():
    return f"Q{REPORT_QUARTER} {REPORT_YEAR}"


# =============================================================================
# TIMESTAMP UTILITIES
# =============================================================================

def timestamp_to_wat(timestamp):
    """
    Convert Unix timestamp to WAT datetime.
    """

    return datetime.fromtimestamp(
        int(timestamp),
        tz=timezone.utc,
    ).astimezone(WAT)


def datetime_to_timestamp(dt):
    """
    Convert timezone-aware datetime to Unix timestamp.
    """

    return int(dt.timestamp())


# =============================================================================
# CANDLE CLASSIFICATION
# =============================================================================

def candle_is_green(candle):
    """
    GREEN candle:
        close > open
    """

    return float(candle["close"]) > float(candle["open"])


def candle_is_red(candle):
    """
    RED candle:
        close < open
    """

    return float(candle["close"]) < float(candle["open"])


def candle_type(candle):
    """
    Return:

        GREEN
        RED
        DOJI
    """

    open_price = float(candle["open"])

    close_price = float(candle["close"])

    if close_price > open_price:
        return "GREEN"

    if close_price < open_price:
        return "RED"

    return "DOJI"


# =============================================================================
# CONNECTION
# =============================================================================

def connect_to_iq_option():
    """
    Connect to IQ Option.
    """

    if not EMAIL or not PASSWORD:
        print(
            "\nERROR: IQ_EMAIL or IQ_PASSWORD is missing "
            "from your .env file."
        )

        sys.exit(1)

    print("\nConnecting to IQ Option...")

    api = IQ_Option(
        EMAIL,
        PASSWORD,
    )

    check, reason = api.connect()

    if not check:
        print("\nConnection failed:")

        print(reason)

        sys.exit(1)

    print("IQ Option connection successful.")

    return api


# =============================================================================
# PRACTICE ACCOUNT
# =============================================================================

def switch_to_practice(api):
    """
    Ensure the API is operating on the practice account.

    This script does not trade, but keeping the connection on PRACTICE
    avoids accidentally using the real account elsewhere.
    """

    try:
        api.change_balance("PRACTICE")

        time.sleep(1)

        print("Account mode: PRACTICE")

    except Exception as exc:
        print(
            "\nWARNING: Could not explicitly switch to PRACTICE:"
        )

        print(
            f"{type(exc).__name__}: {exc}"
        )


# =============================================================================
# DYNAMIC REAL BINARY ACTIVE MAPPINGS
# =============================================================================

def update_real_binary_mappings(api):
    """
    Dynamically retrieve IQ Option Binary active mappings.

    This intentionally does NOT use OTC symbols.

    Required assets:

        USDJPY
        AUDUSD
        EURJPY
        AUDJPY
        GBPJPY
        GBPAUD
        GBPCAD
        CADJPY
    """

    print("\n")
    print("=" * 80)
    print("UPDATING IQ OPTION REAL BINARY ACTIVE MAPPINGS")
    print("=" * 80)

    start = time.time()

    # -------------------------------------------------------------------------
    # First try the package's Binary/Turbo active updater.
    # -------------------------------------------------------------------------

    try:
        print(
            "\nRequesting Binary/Turbo active list from IQ Option..."
        )

        api.get_ALL_Binary_ACTIVES_OPCODE()

        elapsed = time.time() - start

        print(
            f"Active list updated in {elapsed:.2f} seconds."
        )

    except Exception as exc:
        print(
            "\nWARNING: get_ALL_Binary_ACTIVES_OPCODE() failed."
        )

        print(
            f"{type(exc).__name__}: {exc}"
        )

        print(
            "\nAttempting fallback active mapping..."
        )

    # -------------------------------------------------------------------------
    # Fallback:
    # update all active opcodes if the Binary/Turbo updater did not populate
    # the required real forex assets.
    # -------------------------------------------------------------------------

    missing_before_fallback = [
        asset
        for _, asset in CURRENCY_PAIRS
        if stable_api.OP_code.ACTIVES.get(asset) is None
    ]

    if missing_before_fallback:

        try:
            print(
                "\nRunning update_ACTIVES_OPCODE() fallback..."
            )

            api.update_ACTIVES_OPCODE()

        except Exception as exc:

            print(
                "\nWARNING: update_ACTIVES_OPCODE() failed:"
            )

            print(
                f"{type(exc).__name__}: {exc}"
            )

    # -------------------------------------------------------------------------
    # Display results.
    # -------------------------------------------------------------------------

    print("\nRequired REAL Binary forex mappings:")

    print("-" * 80)

    missing = []

    for display_name, asset in CURRENCY_PAIRS:

        active_id = stable_api.OP_code.ACTIVES.get(asset)

        if active_id is None:

            print(
                f"{display_name:<12} "
                f"{asset:<10} -> MISSING"
            )

            missing.append(asset)

        else:

            print(
                f"{display_name:<12} "
                f"{asset:<10} -> {active_id}"
            )

    print("-" * 80)

    # -------------------------------------------------------------------------
    # If some are missing, inspect IQ Option's open Binary assets.
    # -------------------------------------------------------------------------

    if missing:

        print(
            "\nSome mappings are still missing."
        )

        print(
            "Checking IQ Option Binary market list..."
        )

        try:

            open_assets = api.get_all_open_time()

            binary_assets = open_assets.get(
                "binary",
                {},
            )

            turbo_assets = open_assets.get(
                "turbo",
                {},
            )

            print(
                "\nBinary/Turbo availability:"
            )

            for display_name, asset in CURRENCY_PAIRS:

                binary_info = binary_assets.get(
                    asset,
                    {},
                )

                turbo_info = turbo_assets.get(
                    asset,
                    {},
                )

                binary_open = binary_info.get(
                    "open",
                    False,
                )

                turbo_open = turbo_info.get(
                    "open",
                    False,
                )

                active_id = stable_api.OP_code.ACTIVES.get(
                    asset
                )

                print(
                    f"{asset:<10} "
                    f"ID={str(active_id):<8} "
                    f"Binary={str(binary_open):<5} "
                    f"Turbo={str(turbo_open):<5}"
                )

        except Exception as exc:

            print(
                "\nCould not retrieve Binary availability:"
            )

            print(
                f"{type(exc).__name__}: {exc}"
            )

    # -------------------------------------------------------------------------
    # Final validation.
    # -------------------------------------------------------------------------

    missing = [
        asset
        for _, asset in CURRENCY_PAIRS
        if stable_api.OP_code.ACTIVES.get(asset) is None
    ]

    if missing:

        print("\n")
        print("=" * 80)
        print("ERROR: REQUIRED REAL FOREX ASSETS ARE MISSING")
        print("=" * 80)

        for asset in missing:
            print(f"  - {asset}")

        print(
            "\nIMPORTANT:"
        )

        print(
            "This report will NOT replace missing real forex "
            "assets with OTC assets."
        )

        return False

    print("\nAll eight real forex Binary assets are available.")

    return True


# =============================================================================
# RECONNECT
# =============================================================================

def reconnect(api):
    """
    Attempt to reconnect to IQ Option.
    """

    print("\nAttempting IQ Option reconnect...")

    try:

        check, reason = api.connect()

        if check:

            print("Reconnection successful.")

            try:

                api.change_balance(
                    "PRACTICE"
                )

            except Exception:
                pass

            try:

                api.get_ALL_Binary_ACTIVES_OPCODE()

            except Exception as exc:

                print(
                    "Warning: Could not refresh Binary mappings:",
                    exc,
                )

            return True

        print(
            "Reconnect failed:",
            reason,
        )

    except Exception as exc:

        print(
            "Reconnect exception:",
            type(exc).__name__,
            exc,
        )

    return False


# =============================================================================
# FETCH ONE CANDLE BATCH
# =============================================================================

def fetch_candle_batch(
    api,
    asset,
    end_timestamp,
):
    """
    Fetch one batch of historical candles.

    Returns:
        list
        or None if all retries fail.
    """

    for attempt in range(
        1,
        MAX_RETRIES + 1,
    ):

        try:

            candles = api.get_candles(
                asset,
                TIMEFRAME,
                CANDLE_BATCH_SIZE,
                end_timestamp,
            )

            if candles:

                return candles

            print(
                f"  Empty response for {asset} "
                f"(attempt {attempt}/{MAX_RETRIES})"
            )

        except Exception as exc:

            print(
                f"  Candle request failed for {asset} "
                f"(attempt {attempt}/{MAX_RETRIES})"
            )

            print(
                f"  {type(exc).__name__}: {exc}"
            )

            if attempt < MAX_RETRIES:

                time.sleep(
                    RECONNECT_WAIT_SECONDS
                )

                reconnect(api)

    return None


# =============================================================================
# GET CANDLE TIMESTAMP
# =============================================================================

def get_candle_timestamp(candle):
    """
    Extract the candle's Unix timestamp.

    IQ Option normally returns 'from' for the candle start.
    """

    candidates = [
        candle.get("from"),
        candle.get("at"),
        candle.get("timestamp"),
    ]

    for value in candidates:

        if value is None:
            continue

        try:
            return int(value)

        except (
            TypeError,
            ValueError,
        ):
            continue

    return None


# =============================================================================
# DOWNLOAD ONE PAIR
# =============================================================================

def download_pair(
    api,
    display_name,
    asset,
):
    """
    Download every available 1-minute candle for the requested
    real forex pair during the configured quarter.
    """

    print("\n")
    print("=" * 80)
    print(
        f"DOWNLOADING: {display_name}"
    )
    print(
        f"ASSET: {asset}"
    )
    print(
        f"MARKET: REAL FOREX / BINARY"
    )
    print("=" * 80)

    start_timestamp = datetime_to_timestamp(
        START_DATE
    )

    end_timestamp = datetime_to_timestamp(
        END_DATE_EXCLUSIVE
    )

    total_expected = int(
        (
            end_timestamp
            - start_timestamp
        )
        / TIMEFRAME
    )

    print(
        f"Expected theoretical candles: "
        f"{total_expected:,}"
    )

    print(
        f"Period: "
        f"{START_DATE.strftime('%Y-%m-%d %H:%M:%S WAT')} "
        f"-> "
        f"{(END_DATE_EXCLUSIVE - timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S WAT')}"
    )

    print(
        "\nDownloading historical candles..."
    )

    # -------------------------------------------------------------------------
    # timestamp -> candle
    # -------------------------------------------------------------------------

    candle_map = {}

    current_end = end_timestamp - 1

    batch_number = 0

    empty_batches = 0

    while current_end >= start_timestamp:

        batch_number += 1

        candles = fetch_candle_batch(
            api,
            asset,
            current_end,
        )

        # ---------------------------------------------------------------------
        # Empty batch
        # ---------------------------------------------------------------------

        if not candles:

            empty_batches += 1

            print(
                f"  No candles returned. "
                f"Empty batch "
                f"{empty_batches}/{MAX_EMPTY_BATCHES}"
            )

            if (
                empty_batches
                >= MAX_EMPTY_BATCHES
            ):

                print(
                    "\nToo many consecutive empty batches."
                )

                print(
                    "Stopping this pair."
                )

                break

            current_end -= (
                CANDLE_BATCH_SIZE
                * TIMEFRAME
            )

            continue

        empty_batches = 0

        valid_count = 0

        oldest_timestamp = None

        newest_timestamp = None

        # ---------------------------------------------------------------------
        # Process returned candles
        # ---------------------------------------------------------------------

        for candle in candles:

            timestamp = get_candle_timestamp(
                candle
            )

            if timestamp is None:
                continue

            # Ignore candles before requested period.
            if timestamp < start_timestamp:
                continue

            # Ignore candles at/after exclusive end.
            if timestamp >= end_timestamp:
                continue

            candle_map[timestamp] = candle

            valid_count += 1

            if (
                oldest_timestamp is None
                or timestamp < oldest_timestamp
            ):
                oldest_timestamp = timestamp

            if (
                newest_timestamp is None
                or timestamp > newest_timestamp
            ):
                newest_timestamp = timestamp

        # ---------------------------------------------------------------------
        # Progress information
        # ---------------------------------------------------------------------

        oldest_loaded = (
            timestamp_to_wat(
                oldest_timestamp
            )
            if oldest_timestamp is not None
            else None
        )

        newest_loaded = (
            timestamp_to_wat(
                newest_timestamp
            )
            if newest_timestamp is not None
            else None
        )

        print(
            f"Batch {batch_number:<4} | "
            f"received={len(candles):<5} | "
            f"valid={valid_count:<5} | "
            f"total={len(candle_map):,} | "
            f"oldest={oldest_loaded} | "
            f"newest={newest_loaded}"
        )

        # ---------------------------------------------------------------------
        # Move backwards.
        #
        # IMPORTANT:
        #
        # The IQ Option candle documentation recommends using:
        #
        #     oldest candle timestamp - 1
        #
        # as the next end timestamp.
        #
        # This prevents overlapping/duplicate batches and avoids skipping
        # candles.
        # ---------------------------------------------------------------------

        if oldest_timestamp is not None:

            next_end = (
                oldest_timestamp - 1
            )

            if next_end >= current_end:

                next_end = (
                    current_end
                    - (
                        CANDLE_BATCH_SIZE
                        * TIMEFRAME
                    )
                )

            current_end = next_end

        else:

            current_end -= (
                CANDLE_BATCH_SIZE
                * TIMEFRAME
            )

        time.sleep(
            REQUEST_DELAY_SECONDS
        )

    # -------------------------------------------------------------------------
    # Download complete
    # -------------------------------------------------------------------------

    print("\nDownload complete.")

    print(
        f"Total unique candles collected: "
        f"{len(candle_map):,}"
    )

    if candle_map:

        first_timestamp = min(
            candle_map.keys()
        )

        last_timestamp = max(
            candle_map.keys()
        )

        print(
            "Actual range: "
            f"{timestamp_to_wat(first_timestamp)} "
            f"-> "
            f"{timestamp_to_wat(last_timestamp)}"
        )

    return candle_map


# =============================================================================
# SAVE RAW CANDLES
# =============================================================================

def save_candles(
    asset,
    candle_map,
):
    """
    Save downloaded candles to CSV.
    """

    filename = (
        f"{asset}_"
        f"{START_DATE.strftime('%Y-%m-%d')}_"
        f"{(END_DATE_EXCLUSIVE - timedelta(seconds=1)).strftime('%Y-%m-%d')}"
        ".csv"
    )

    filepath = DATA_DIR / filename

    sorted_candles = sorted(
        candle_map.items(),
        key=lambda item: item[0],
    )

    with filepath.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.writer(file)

        writer.writerow(
            [
                "timestamp",
                "datetime_wat",
                "open",
                "close",
                "high",
                "low",
                "volume",
                "candle_type",
            ]
        )

        for timestamp, candle in sorted_candles:

            writer.writerow(
                [
                    timestamp,

                    timestamp_to_wat(
                        timestamp
                    ).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),

                    candle.get("open"),

                    candle.get("close"),

                    candle.get("max"),

                    candle.get("min"),

                    candle.get("volume"),

                    candle_type(candle),
                ]
            )

    print(
        "\nSaved candle data:"
        f"\n{filepath}"
    )

    return filepath


# =============================================================================
# STREAK ANALYSIS
# =============================================================================

def analyze_streaks(candle_map):
    """
    Analyze consecutive GREEN and RED candle streaks.

    A DOJI breaks a streak.

    Returns complete streak statistics.
    """

    if not candle_map:

        return {
            "total_candles": 0,
            "green_candles": 0,
            "red_candles": 0,
            "doji_candles": 0,
            "longest_green": 0,
            "longest_red": 0,
            "green_streaks_7_plus": 0,
            "red_streaks_7_plus": 0,
            "green_streaks": [],
            "red_streaks": [],
            "longest_green_start": None,
            "longest_green_end": None,
            "longest_red_start": None,
            "longest_red_end": None,
        }

    sorted_candles = sorted(
        candle_map.items(),
        key=lambda item: item[0],
    )

    current_color = None

    current_length = 0

    current_start = None

    current_end = None

    green_count = 0

    red_count = 0

    doji_count = 0

    green_streaks = []

    red_streaks = []

    # -------------------------------------------------------------------------
    # Close current streak
    # -------------------------------------------------------------------------

    def close_streak():

        nonlocal current_color

        nonlocal current_length

        nonlocal current_start

        nonlocal current_end

        if current_color == "GREEN":

            green_streaks.append(
                {
                    "length": current_length,
                    "start": current_start,
                    "end": current_end,
                }
            )

        elif current_color == "RED":

            red_streaks.append(
                {
                    "length": current_length,
                    "start": current_start,
                    "end": current_end,
                }
            )

    # -------------------------------------------------------------------------
    # Process candles chronologically
    # -------------------------------------------------------------------------

    for timestamp, candle in sorted_candles:

        color = candle_type(candle)

        # ---------------------------------------------------------------------
        # Count candle colors
        # ---------------------------------------------------------------------

        if color == "GREEN":

            green_count += 1

        elif color == "RED":

            red_count += 1

        else:

            doji_count += 1

        # ---------------------------------------------------------------------
        # DOJI breaks the streak.
        # ---------------------------------------------------------------------

        if color == "DOJI":

            close_streak()

            current_color = None

            current_length = 0

            current_start = None

            current_end = None

            continue

        # ---------------------------------------------------------------------
        # Same color continues streak.
        # ---------------------------------------------------------------------

        if color == current_color:

            current_length += 1

            current_end = timestamp

        else:

            # Previous streak finished.
            close_streak()

            current_color = color

            current_length = 1

            current_start = timestamp

            current_end = timestamp

    # -------------------------------------------------------------------------
    # Close final streak.
    # -------------------------------------------------------------------------

    close_streak()

    # -------------------------------------------------------------------------
    # Longest streaks
    # -------------------------------------------------------------------------

    longest_green = (
        max(
            streak["length"]
            for streak in green_streaks
        )
        if green_streaks
        else 0
    )

    longest_red = (
        max(
            streak["length"]
            for streak in red_streaks
        )
        if red_streaks
        else 0
    )

    # -------------------------------------------------------------------------
    # Number of 7+ streaks
    # -------------------------------------------------------------------------

    green_streaks_7_plus = sum(
        1
        for streak in green_streaks
        if streak["length"]
        >= STREAK_LENGTH_REQUIRED
    )

    red_streaks_7_plus = sum(
        1
        for streak in red_streaks
        if streak["length"]
        >= STREAK_LENGTH_REQUIRED
    )

    # -------------------------------------------------------------------------
    # Longest green details
    # -------------------------------------------------------------------------

    longest_green_start = None

    longest_green_end = None

    if green_streaks:

        longest_green_entry = max(
            green_streaks,
            key=lambda item: item["length"],
        )

        longest_green_start = (
            longest_green_entry["start"]
        )

        longest_green_end = (
            longest_green_entry["end"]
        )

    # -------------------------------------------------------------------------
    # Longest red details
    # -------------------------------------------------------------------------

    longest_red_start = None

    longest_red_end = None

    if red_streaks:

        longest_red_entry = max(
            red_streaks,
            key=lambda item: item["length"],
        )

        longest_red_start = (
            longest_red_entry["start"]
        )

        longest_red_end = (
            longest_red_entry["end"]
        )

    # -------------------------------------------------------------------------
    # Return statistics
    # -------------------------------------------------------------------------

    return {
        "total_candles": len(candle_map),

        "green_candles": green_count,

        "red_candles": red_count,

        "doji_candles": doji_count,

        "longest_green": longest_green,

        "longest_red": longest_red,

        "green_streaks_7_plus": green_streaks_7_plus,

        "red_streaks_7_plus": red_streaks_7_plus,

        "green_streaks": green_streaks,

        "red_streaks": red_streaks,

        "longest_green_start": longest_green_start,

        "longest_green_end": longest_green_end,

        "longest_red_start": longest_red_start,

        "longest_red_end": longest_red_end,
    }


# =============================================================================
# PRINT LONGEST STREAK
# =============================================================================

def print_longest_streak(
    label,
    streak,
):
    """
    Print detailed information about a streak.
    """

    if not streak:
        return

    start = timestamp_to_wat(
        streak["start"]
    )

    end = timestamp_to_wat(
        streak["end"]
    )

    print(
        f"\n{label}:"
    )

    print(
        f"  Length: {streak['length']} candles"
    )

    print(
        f"  Start:  {start}"
    )

    print(
        f"  End:    {end}"
    )


# =============================================================================
# PRINT ALL 7+ STREAKS
# =============================================================================

def print_qualified_streaks(
    color,
    streaks,
):
    """
    Print every streak meeting the strategy threshold.
    """

    qualified = [
        streak
        for streak in streaks
        if streak["length"]
        >= STREAK_LENGTH_REQUIRED
    ]

    if not qualified:

        print(
            f"\nNo {color} streaks of "
            f"{STREAK_LENGTH_REQUIRED}+ candles."
        )

        return

    print("\n")

    print(
        "=" * 80
    )

    print(
        f"{color} STREAKS >= "
        f"{STREAK_LENGTH_REQUIRED}"
    )

    print(
        "=" * 80
    )

    print(
        f"{'#':<5}"
        f"{'LENGTH':<10}"
        f"{'START WAT':<22}"
        f"{'END WAT':<22}"
    )

    print(
        "-" * 80
    )

    for index, streak in enumerate(
        qualified,
        start=1,
    ):

        start = timestamp_to_wat(
            streak["start"]
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        end = timestamp_to_wat(
            streak["end"]
        ).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

        print(
            f"{index:<5}"
            f"{streak['length']:<10}"
            f"{start:<22}"
            f"{end:<22}"
        )


# =============================================================================
# PRINT ANALYSIS
# =============================================================================

def print_analysis(
    display_name,
    asset,
    stats,
):
    """
    Print detailed analysis for one pair.
    """

    print("\n")

    print(
        "=" * 80
    )

    print(
        f"ANALYSIS: {display_name}"
    )

    print(
        "=" * 80
    )

    print(
        f"Asset:                 {asset}"
    )

    print(
        "Market:                REAL FOREX / BINARY"
    )

    print(
        f"Quarter:               {quarter_name()}"
    )

    print(
        f"Total candles:         "
        f"{stats['total_candles']:,}"
    )

    print(
        f"GREEN candles:         "
        f"{stats['green_candles']:,}"
    )

    print(
        f"RED candles:           "
        f"{stats['red_candles']:,}"
    )

    print(
        f"DOJI candles:          "
        f"{stats['doji_candles']:,}"
    )

    print("-" * 80)

    print(
        f"Longest GREEN streak:  "
        f"{stats['longest_green']}"
    )

    print(
        f"Longest RED streak:    "
        f"{stats['longest_red']}"
    )

    print("-" * 80)

    print(
        f"GREEN streaks >= "
        f"{STREAK_LENGTH_REQUIRED}: "
        f"{stats['green_streaks_7_plus']}"
    )

    print(
        f"RED streaks >= "
        f"{STREAK_LENGTH_REQUIRED}:   "
        f"{stats['red_streaks_7_plus']}"
    )

    # -------------------------------------------------------------------------
    # Longest green
    # -------------------------------------------------------------------------

    if stats["longest_green_start"]:

        print(
            "\nLongest GREEN:"
        )

        print(
            f"  Start: "
            f"{timestamp_to_wat(stats['longest_green_start'])}"
        )

        print(
            f"  End:   "
            f"{timestamp_to_wat(stats['longest_green_end'])}"
        )

    # -------------------------------------------------------------------------
    # Longest red
    # -------------------------------------------------------------------------

    if stats["longest_red_start"]:

        print(
            "\nLongest RED:"
        )

        print(
            f"  Start: "
            f"{timestamp_to_wat(stats['longest_red_start'])}"
        )

        print(
            f"  End:   "
            f"{timestamp_to_wat(stats['longest_red_end'])}"
        )

    # -------------------------------------------------------------------------
    # Qualified streaks
    # -------------------------------------------------------------------------

    print_qualified_streaks(
        "GREEN",
        stats["green_streaks"],
    )

    print_qualified_streaks(
        "RED",
        stats["red_streaks"],
    )


# =============================================================================
# SAVE SUMMARY REPORT
# =============================================================================

def save_summary_report(results):
    """
    Save final quarterly summary to CSV.
    """

    filename = (
        f"firefly_{quarter_name().replace(' ', '_')}"
        f"_real_binary_forex_streak_report.csv"
    )

    filepath = REPORT_DIR / filename

    with filepath.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.writer(file)

        writer.writerow(
            [
                "quarter",
                "pair",
                "asset",
                "market",
                "total_candles",
                "green_candles",
                "red_candles",
                "doji_candles",
                "longest_green",
                "longest_red",
                "green_streaks_7_plus",
                "red_streaks_7_plus",
                "longest_green_start_wat",
                "longest_green_end_wat",
                "longest_red_start_wat",
                "longest_red_end_wat",
            ]
        )

        for result in results:

            stats = result["stats"]

            green_start = (
                timestamp_to_wat(
                    stats["longest_green_start"]
                ).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if stats["longest_green_start"]
                else ""
            )

            green_end = (
                timestamp_to_wat(
                    stats["longest_green_end"]
                ).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if stats["longest_green_end"]
                else ""
            )

            red_start = (
                timestamp_to_wat(
                    stats["longest_red_start"]
                ).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if stats["longest_red_start"]
                else ""
            )

            red_end = (
                timestamp_to_wat(
                    stats["longest_red_end"]
                ).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                if stats["longest_red_end"]
                else ""
            )

            writer.writerow(
                [
                    quarter_name(),

                    result["display_name"],

                    result["asset"],

                    "REAL FOREX / BINARY",

                    stats["total_candles"],

                    stats["green_candles"],

                    stats["red_candles"],

                    stats["doji_candles"],

                    stats["longest_green"],

                    stats["longest_red"],

                    stats["green_streaks_7_plus"],

                    stats["red_streaks_7_plus"],

                    green_start,

                    green_end,

                    red_start,

                    red_end,
                ]
            )

    print(
        "\nSummary report saved to:"
        f"\n{filepath}"
    )

    return filepath


# =============================================================================
# SAVE ALL QUALIFIED STREAKS
# =============================================================================

def save_streak_report(results):
    """
    Save every 7+ GREEN and RED streak to a separate CSV.

    This is particularly useful for validating the Firefly strategy.
    """

    filename = (
        f"firefly_{quarter_name().replace(' ', '_')}"
        f"_qualified_streaks.csv"
    )

    filepath = REPORT_DIR / filename

    with filepath.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.writer(file)

        writer.writerow(
            [
                "quarter",
                "pair",
                "asset",
                "market",
                "color",
                "streak_length",
                "start_wat",
                "end_wat",
                "strategy_signal",
            ]
        )

        for result in results:

            for color, streaks in (
                (
                    "GREEN",
                    result["stats"]["green_streaks"],
                ),
                (
                    "RED",
                    result["stats"]["red_streaks"],
                ),
            ):

                for streak in streaks:

                    if (
                        streak["length"]
                        < STREAK_LENGTH_REQUIRED
                    ):
                        continue

                    start = timestamp_to_wat(
                        streak["start"]
                    ).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )

                    end = timestamp_to_wat(
                        streak["end"]
                    ).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )

                    # Firefly strategy:
                    #
                    # 7 GREEN -> SELL / LOWER
                    # 7 RED   -> BUY / HIGHER

                    if color == "GREEN":
                        signal = "SELL / LOWER"

                    else:
                        signal = "BUY / HIGHER"

                    writer.writerow(
                        [
                            quarter_name(),

                            result["display_name"],

                            result["asset"],

                            "REAL FOREX / BINARY",

                            color,

                            streak["length"],

                            start,

                            end,

                            signal,
                        ]
                    )

    print(
        "\nQualified streak report saved to:"
        f"\n{filepath}"
    )

    return filepath


# =============================================================================
# FINAL SUMMARY
# =============================================================================

def print_final_summary(results):
    """
    Print compact final quarterly report.
    """

    print("\n\n")

    print(
        "=" * 120
    )

    print(
        "FIREFLY AI - QUARTERLY REAL FOREX BINARY REPORT"
    )

    print(
        "=" * 120
    )

    print(
        f"Quarter: {quarter_name()}"
    )

    print(
        f"Period: "
        f"{START_DATE.strftime('%Y-%m-%d')} "
        f"to "
        f"{(END_DATE_EXCLUSIVE - timedelta(seconds=1)).strftime('%Y-%m-%d')}"
    )

    print(
        "Market: REAL FOREX / BINARY"
    )

    print(
        "Timeframe: 1 minute"
    )

    print(
        f"Streak trigger: "
        f"{STREAK_LENGTH_REQUIRED} consecutive candles"
    )

    print(
        "=" * 120
    )

    header = (
        f"{'PAIR':<12}"
        f"{'CANDLES':>12}"
        f"{'GREEN':>12}"
        f"{'RED':>12}"
        f"{'DOJI':>10}"
        f"{'MAX GREEN':>13}"
        f"{'MAX RED':>11}"
        f"{'GREEN >=7':>13}"
        f"{'RED >=7':>11}"
    )

    print(header)

    print(
        "-" * 120
    )

    for result in results:

        stats = result["stats"]

        print(
            f"{result['display_name']:<12}"
            f"{stats['total_candles']:>12,}"
            f"{stats['green_candles']:>12,}"
            f"{stats['red_candles']:>12,}"
            f"{stats['doji_candles']:>10,}"
            f"{stats['longest_green']:>13}"
            f"{stats['longest_red']:>11}"
            f"{stats['green_streaks_7_plus']:>13}"
            f"{stats['red_streaks_7_plus']:>11}"
        )

    print(
        "=" * 120
    )

    # -------------------------------------------------------------------------
    # Strategy interpretation
    # -------------------------------------------------------------------------

    print(
        "\nFIREFLY STRATEGY INTERPRETATION:"
    )

    print(
        f"  {STREAK_LENGTH_REQUIRED} GREEN candles "
        "-> SELL / LOWER"
    )

    print(
        f"  {STREAK_LENGTH_REQUIRED} RED candles "
        "-> BUY / HIGHER"
    )

    print(
        "\nNOTE:"
    )

    print(
        "This report measures historical candle behavior."
    )

    print(
        "It does NOT establish that the next candle will "
        "necessarily reverse."
    )

    print(
        "\n"
    )


# =============================================================================
# MAIN
# =============================================================================

def main():

    print(
        "=" * 100
    )

    print(
        "FIREFLY AI - QUARTERLY REAL FOREX BINARY CANDLE REPORT"
    )

    print(
        "=" * 100
    )

    # -------------------------------------------------------------------------
    # Report information
    # -------------------------------------------------------------------------

    print(
        f"\nQuarter: {quarter_name()}"
    )

    print(
        f"From: "
        f"{START_DATE.strftime('%Y-%m-%d %H:%M:%S WAT')}"
    )

    print(
        f"To:   "
        f"{(END_DATE_EXCLUSIVE - timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S WAT')}"
    )

    print(
        "\nMarket: REAL FOREX / BINARY"
    )

    print(
        "Timeframe: 1 minute"
    )

    print(
        f"Number of pairs: "
        f"{len(CURRENCY_PAIRS)}"
    )

    # -------------------------------------------------------------------------
    # Expected theoretical candles
    # -------------------------------------------------------------------------

    expected_candles = int(
        (
            datetime_to_timestamp(
                END_DATE_EXCLUSIVE
            )
            -
            datetime_to_timestamp(
                START_DATE
            )
        )
        / TIMEFRAME
    )

    print(
        "\nExpected theoretical one-minute candles "
        f"per pair: {expected_candles:,}"
    )

    print(
        "Expected theoretical maximum across "
        f"all eight pairs: "
        f"{expected_candles * len(CURRENCY_PAIRS):,}"
    )

    print(
        "\nNOTE:"
    )

    print(
        "The expected number is theoretical. "
        "The actual IQ Option feed may contain "
        "gaps or unavailable candles."
    )

    # -------------------------------------------------------------------------
    # Connect
    # -------------------------------------------------------------------------

    api = connect_to_iq_option()

    # -------------------------------------------------------------------------
    # Practice account
    # -------------------------------------------------------------------------

    switch_to_practice(api)

    try:

        balance = api.get_balance()

        print(
            f"Practice balance: ${balance:,.2f}"
        )

    except Exception as exc:

        print(
            "Could not retrieve balance:",
            exc,
        )

    # -------------------------------------------------------------------------
    # Dynamic real Binary mappings
    # -------------------------------------------------------------------------

    if not update_real_binary_mappings(api):

        print(
            "\nCannot continue because one or more "
            "real forex Binary assets could not be resolved."
        )

        print(
            "\nNo OTC substitutions will be made."
        )

        sys.exit(1)

    # -------------------------------------------------------------------------
    # Process pairs
    # -------------------------------------------------------------------------

    results = []

    for index, (
        display_name,
        asset,
    ) in enumerate(
        CURRENCY_PAIRS,
        start=1,
    ):

        print("\n")

        print(
            "#" * 100
        )

        print(
            f"# PAIR {index}/{len(CURRENCY_PAIRS)}"
        )

        print(
            f"# {display_name}"
        )

        print(
            f"# REAL ASSET: {asset}"
        )

        print(
            "# MARKET: BINARY"
        )

        print(
            "#" * 100
        )

        # ---------------------------------------------------------------------
        # Verify active ID
        # ---------------------------------------------------------------------

        active_id = stable_api.OP_code.ACTIVES.get(
            asset
        )

        if active_id is None:

            print(
                f"\nSKIPPING {asset}: "
                "no active ID found."
            )

            continue

        print(
            f"Using IQ Option active ID: "
            f"{active_id}"
        )

        # ---------------------------------------------------------------------
        # Download
        # ---------------------------------------------------------------------

        candle_map = download_pair(
            api,
            display_name,
            asset,
        )

        if not candle_map:

            print(
                f"\nWARNING: No candle data obtained "
                f"for {asset}."
            )

            continue

        # ---------------------------------------------------------------------
        # Save raw data
        # ---------------------------------------------------------------------

        save_candles(
            asset,
            candle_map,
        )

        # ---------------------------------------------------------------------
        # Analyze
        # ---------------------------------------------------------------------

        stats = analyze_streaks(
            candle_map
        )

        # ---------------------------------------------------------------------
        # Print analysis
        # ---------------------------------------------------------------------

        print_analysis(
            display_name,
            asset,
            stats,
        )

        # ---------------------------------------------------------------------
        # Store result
        # ---------------------------------------------------------------------

        results.append(
            {
                "display_name": display_name,
                "asset": asset,
                "stats": stats,
            }
        )

    # -------------------------------------------------------------------------
    # Final report
    # -------------------------------------------------------------------------

    if results:

        print_final_summary(
            results
        )

        save_summary_report(
            results
        )

        save_streak_report(
            results
        )

    else:

        print(
            "\nERROR: No pairs were successfully analyzed."
        )

        sys.exit(1)

    # -------------------------------------------------------------------------
    # Complete
    # -------------------------------------------------------------------------

    print("\n")

    print(
        "=" * 100
    )

    print(
        "QUARTERLY REPORT COMPLETE"
    )

    print(
        "=" * 100
    )

    print(
        "\nRaw candle data directory:"
        f"\n{DATA_DIR}"
    )

    print(
        "\nSummary report directory:"
        f"\n{REPORT_DIR}"
    )

    print("\n")


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":

    try:

        main()

    except KeyboardInterrupt:

        print(
            "\n\nProgram interrupted by user."
        )

        print(
            "Already downloaded data may have been "
            "saved for completed pairs."
        )

        sys.exit(1)

    except Exception as exc:

        print(
            "\n\nFATAL ERROR:"
        )

        print(
            f"{type(exc).__name__}: {exc}"
        )

        logging.exception(
            "Unhandled exception"
        )

        sys.exit(1)

