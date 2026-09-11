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
# FIREFLY AI - FOUR-MONTH CANDLE REPORT
# =============================================================================
#
# PURPOSE:
#   Download and analyze 1-minute OTC candles for:
#
#       May 1, 2026 -> August 31, 2026
#
#   Requested pairs:
#
#       USDJPY-OTC
#       AUDUSD-OTC
#       EURJPY-OTC
#       AUDJPY-OTC
#       GBPJPY-OTC
#       GBPAUD-OTC
#       GBPCAD-OTC
#       CADJPY-OTC
#
# IMPORTANT:
#   OTC asset IDs are dynamically retrieved from IQ Option's
#   Binary/Turbo active list.
#
#   DO NOT hard-code the OTC IDs from constants.py.
#
# =============================================================================


# =============================================================================
# CONFIGURATION
# =============================================================================

TIMEFRAME = 60                    # 1-minute candles
CANDLE_BATCH_SIZE = 500           # IQ Option request size
MONTHS_BACK = 4

STREAK_LENGTH_REQUIRED = 7

MAX_RETRIES = 5
RECONNECT_WAIT_SECONDS = 5

# Delay between candle requests.
# A small delay helps avoid overwhelming the API.
REQUEST_DELAY_SECONDS = 0.25

# How many consecutive empty/failed batches are tolerated
MAX_EMPTY_BATCHES = 3

# Timezone used for displaying the report.
# Nigeria / West Africa Time = UTC+1
WAT = timezone(timedelta(hours=1))


# =============================================================================
# PAIRS
# =============================================================================

CURRENCY_PAIRS = [
    ("USD/JPY OTC", "USDJPY-OTC"),
    ("AUD/USD OTC", "AUDUSD-OTC"),
    ("EUR/JPY OTC", "EURJPY-OTC"),
    ("AUD/JPY OTC", "AUDJPY-OTC"),
    ("GBP/JPY OTC", "GBPJPY-OTC"),
    ("GBP/AUD OTC", "GBPAUD-OTC"),
    ("GBP/CAD OTC", "GBPCAD-OTC"),
    ("CAD/JPY OTC", "CADJPY-OTC"),
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
# DATE RANGE
# =============================================================================
#
# Four complete calendar months before September 2026:
#
# May 1, 2026
# June
# July
# August 31, 2026
#
# We use an exclusive end timestamp of September 1, 2026.
#
# This makes the candle filtering precise.
# =============================================================================

START_DATE = datetime(
    2026,
    5,
    1,
    0,
    0,
    0,
    tzinfo=WAT,
)

END_DATE_EXCLUSIVE = datetime(
    2026,
    9,
    1,
    0,
    0,
    0,
    tzinfo=WAT,
)


# =============================================================================
# UTILITY FUNCTIONS
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
    Return GREEN, RED, or DOJI.
    """

    open_price = float(candle["open"])
    close_price = float(candle["close"])

    if close_price > open_price:
        return "GREEN"

    if close_price < open_price:
        return "RED"

    return "DOJI"


# =============================================================================
# IQ OPTION CONNECTION
# =============================================================================

def connect_to_iq_option():
    """
    Connect to IQ Option.
    """

    if not EMAIL or not PASSWORD:
        print("ERROR: IQ_EMAIL or IQ_PASSWORD is missing from .env")
        sys.exit(1)

    print("\nConnecting to IQ Option...")

    api = IQ_Option(
        EMAIL,
        PASSWORD,
    )

    check, reason = api.connect()

    if not check:
        print("Connection failed:")
        print(reason)
        sys.exit(1)

    print("IQ Option connection successful.")

    return api


# =============================================================================
# DYNAMIC OTC ACTIVE MAPPINGS
# =============================================================================

def update_otc_mappings(api):
    """
    Retrieve the current Binary/Turbo active list from IQ Option.

    This is critical because the installed constants.py does not contain
    all current OTC instruments.

    The confirmed dynamic mappings from our testing include:

        AUDUSD-OTC -> 2111
        AUDJPY-OTC -> 2113
        GBPAUD-OTC -> 2116
        GBPCAD-OTC -> 2114
        CADJPY-OTC -> 2136

    We intentionally do NOT hard-code those values.
    """

    print("\n")
    print("=" * 80)
    print("UPDATING IQ OPTION OTC ACTIVE MAPPINGS")
    print("=" * 80)

    start = time.time()

    try:
        api.get_ALL_Binary_ACTIVES_OPCODE()

    except Exception as exc:
        print("\nERROR updating Binary/Turbo active list:")
        print(type(exc).__name__, exc)
        return False

    elapsed = time.time() - start

    print(f"\nActive list updated in {elapsed:.2f} seconds.")

    print("\nRequired OTC mappings:")
    print("-" * 80)

    missing = []

    for display_name, asset in CURRENCY_PAIRS:

        active_id = stable_api.OP_code.ACTIVES.get(asset)

        if active_id is None:
            print(f"{asset:<20} -> MISSING")
            missing.append(asset)
        else:
            print(f"{asset:<20} -> {active_id}")

    if missing:

        print("\n")
        print("=" * 80)
        print("ERROR: REQUIRED OTC ASSETS ARE MISSING")
        print("=" * 80)

        for asset in missing:
            print(f"  - {asset}")

        return False

    print("\nAll required OTC assets are available.")

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

            # Refresh binary/turbo mappings after reconnect.
            try:
                api.get_ALL_Binary_ACTIVES_OPCODE()
            except Exception as exc:
                print(
                    "Warning: Could not refresh active mappings:",
                    exc,
                )

            return True

        print("Reconnect failed:", reason)

    except Exception as exc:
        print(
            "Reconnect exception:",
            type(exc).__name__,
            exc,
        )

    return False


# =============================================================================
# FETCH CANDLES WITH RETRIES
# =============================================================================

def fetch_candle_batch(api, asset, end_timestamp):
    """
    Fetch one batch of candles.

    Returns:
        list of candles
        or None when the request failed
    """

    for attempt in range(1, MAX_RETRIES + 1):

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

                time.sleep(RECONNECT_WAIT_SECONDS)

                if not reconnect(api):
                    continue

    return None


# =============================================================================
# DOWNLOAD ONE PAIR
# =============================================================================

def download_pair(api, display_name, asset):
    """
    Download all candles for one pair between START_DATE and
    END_DATE_EXCLUSIVE.
    """

    print("\n")
    print("=" * 80)
    print(f"DOWNLOADING: {display_name}")
    print(f"ASSET: {asset}")
    print("=" * 80)

    start_timestamp = datetime_to_timestamp(START_DATE)
    end_timestamp = datetime_to_timestamp(END_DATE_EXCLUSIVE)

    total_expected = int(
        (end_timestamp - start_timestamp) / TIMEFRAME
    )

    print(
        f"Expected maximum candles: approximately {total_expected:,}"
    )

    print(
        f"Period: "
        f"{START_DATE.strftime('%Y-%m-%d %H:%M:%S WAT')} "
        f"-> "
        f"{(END_DATE_EXCLUSIVE - timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S WAT')}"
    )

    # Timestamp -> candle
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

        if not candles:

            empty_batches += 1

            print(
                f"  No candles returned. "
                f"Empty batch {empty_batches}/{MAX_EMPTY_BATCHES}"
            )

            if empty_batches >= MAX_EMPTY_BATCHES:

                print(
                    "\nToo many consecutive empty batches."
                )

                print(
                    "Stopping this pair to avoid an infinite loop."
                )

                break

            current_end -= (
                CANDLE_BATCH_SIZE * TIMEFRAME
            )

            continue

        empty_batches = 0

        valid_count = 0

        oldest_timestamp = None
        newest_timestamp = None

        for candle in candles:

            try:
                timestamp = int(
                    candle.get("from")
                    or candle.get("at")
                    or candle.get("timestamp")
                )

            except Exception:
                continue

            if timestamp < start_timestamp:
                continue

            if timestamp >= end_timestamp:
                continue

            candle_map[timestamp] = candle

            valid_count += 1

            if oldest_timestamp is None:
                oldest_timestamp = timestamp
            else:
                oldest_timestamp = min(
                    oldest_timestamp,
                    timestamp,
                )

            if newest_timestamp is None:
                newest_timestamp = timestamp
            else:
                newest_timestamp = max(
                    newest_timestamp,
                    timestamp,
                )

        # Display progress.
        oldest_loaded = (
            timestamp_to_wat(oldest_timestamp)
            if oldest_timestamp
            else None
        )

        newest_loaded = (
            timestamp_to_wat(newest_timestamp)
            if newest_timestamp
            else None
        )

        print(
            f"Batch {batch_number:<5} | "
            f"received={len(candles):<4} | "
            f"valid={valid_count:<4} | "
            f"total={len(candle_map):,} | "
            f"oldest={oldest_loaded}"
        )

        # Move backward.
        if oldest_timestamp is not None:

            next_end = (
                oldest_timestamp - TIMEFRAME
            )

            if next_end >= current_end:

                # Safety against the API returning the same batch.
                next_end = (
                    current_end
                    - CANDLE_BATCH_SIZE * TIMEFRAME
                )

            current_end = next_end

        else:

            current_end -= (
                CANDLE_BATCH_SIZE * TIMEFRAME
            )

        time.sleep(REQUEST_DELAY_SECONDS)

    print("\nDownload complete.")

    print(
        f"Total unique candles collected: "
        f"{len(candle_map):,}"
    )

    if candle_map:

        first_timestamp = min(candle_map.keys())
        last_timestamp = max(candle_map.keys())

        print(
            "Actual range: "
            f"{timestamp_to_wat(first_timestamp)} "
            f"-> "
            f"{timestamp_to_wat(last_timestamp)}"
        )

    return candle_map


# =============================================================================
# SAVE CANDLES TO CSV
# =============================================================================

def save_candles(asset, candle_map):
    """
    Save downloaded candles to CSV.
    """

    filename = (
        f"{asset}_"
        f"{START_DATE.strftime('%Y-%m-%d')}_"
        f"{(END_DATE_EXCLUSIVE - timedelta(seconds=1)).strftime('%Y-%m-%d')}.csv"
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
                    timestamp_to_wat(timestamp).strftime(
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
        f"\nSaved candle data:"
        f"\n{filepath}"
    )

    return filepath


# =============================================================================
# STREAK ANALYSIS
# =============================================================================

def analyze_streaks(candle_map):
    """
    Analyze consecutive GREEN and RED candle streaks.

    DOJI breaks a streak.

    Returns detailed statistics.
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
        }

    sorted_candles = sorted(
        candle_map.items(),
        key=lambda item: item[0],
    )

    current_color = None
    current_length = 0
    current_start = None

    longest_green = 0
    longest_red = 0

    green_count = 0
    red_count = 0
    doji_count = 0

    green_streaks = []
    red_streaks = []

    def close_streak():

        nonlocal current_color
        nonlocal current_length
        nonlocal current_start

        if current_color == "GREEN":

            green_streaks.append(
                {
                    "length": current_length,
                    "start": current_start,
                }
            )

        elif current_color == "RED":

            red_streaks.append(
                {
                    "length": current_length,
                    "start": current_start,
                }
            )

    for timestamp, candle in sorted_candles:

        color = candle_type(candle)

        if color == "GREEN":
            green_count += 1

        elif color == "RED":
            red_count += 1

        else:
            doji_count += 1

        # DOJI breaks everything.
        if color == "DOJI":

            close_streak()

            current_color = None
            current_length = 0
            current_start = None

            continue

        # Same color continues streak.
        if color == current_color:

            current_length += 1

        else:

            # Previous streak is finished.
            close_streak()

            current_color = color
            current_length = 1
            current_start = timestamp

    # Close final streak.
    close_streak()

    # Longest streaks.
    if green_streaks:

        longest_green = max(
            streak["length"]
            for streak in green_streaks
        )

    if red_streaks:

        longest_red = max(
            streak["length"]
            for streak in red_streaks
        )

    green_streaks_7_plus = sum(
        1
        for streak in green_streaks
        if streak["length"] >= STREAK_LENGTH_REQUIRED
    )

    red_streaks_7_plus = sum(
        1
        for streak in red_streaks
        if streak["length"] >= STREAK_LENGTH_REQUIRED
    )

    # Longest streak details.
    longest_green_start = None
    longest_red_start = None

    if green_streaks:

        longest_green_entry = max(
            green_streaks,
            key=lambda item: item["length"],
        )

        longest_green_start = (
            longest_green_entry["start"]
        )

    if red_streaks:

        longest_red_entry = max(
            red_streaks,
            key=lambda item: item["length"],
        )

        longest_red_start = (
            longest_red_entry["start"]
        )

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

        "longest_red_start": longest_red_start,
    }


# =============================================================================
# PRINT ANALYSIS
# =============================================================================

def print_analysis(display_name, asset, stats):
    """
    Print analysis for one pair.
    """

    print("\n")
    print("=" * 80)
    print(f"ANALYSIS: {display_name}")
    print("=" * 80)

    print(f"Asset:                 {asset}")
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
        f"GREEN streaks >= {STREAK_LENGTH_REQUIRED}: "
        f"{stats['green_streaks_7_plus']}"
    )

    print(
        f"RED streaks >= {STREAK_LENGTH_REQUIRED}:   "
        f"{stats['red_streaks_7_plus']}"
    )

    if stats["longest_green_start"]:

        print(
            "Longest GREEN started: "
            f"{timestamp_to_wat(stats['longest_green_start'])}"
        )

    if stats["longest_red_start"]:

        print(
            "Longest RED started:   "
            f"{timestamp_to_wat(stats['longest_red_start'])}"
        )


# =============================================================================
# SAVE SUMMARY REPORT
# =============================================================================

def save_summary_report(results):
    """
    Save final summary to CSV.
    """

    filepath = (
        REPORT_DIR
        / "firefly_four_month_streak_report.csv"
    )

    with filepath.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.writer(file)

        writer.writerow(
            [
                "pair",
                "asset",
                "total_candles",
                "green_candles",
                "red_candles",
                "doji_candles",
                "longest_green",
                "longest_red",
                "green_streaks_7_plus",
                "red_streaks_7_plus",
                "longest_green_start_wat",
                "longest_red_start_wat",
            ]
        )

        for result in results:

            stats = result["stats"]

            green_start = (
                timestamp_to_wat(
                    stats["longest_green_start"]
                ).strftime("%Y-%m-%d %H:%M:%S")
                if stats["longest_green_start"]
                else ""
            )

            red_start = (
                timestamp_to_wat(
                    stats["longest_red_start"]
                ).strftime("%Y-%m-%d %H:%M:%S")
                if stats["longest_red_start"]
                else ""
            )

            writer.writerow(
                [
                    result["display_name"],
                    result["asset"],
                    stats["total_candles"],
                    stats["green_candles"],
                    stats["red_candles"],
                    stats["doji_candles"],
                    stats["longest_green"],
                    stats["longest_red"],
                    stats["green_streaks_7_plus"],
                    stats["red_streaks_7_plus"],
                    green_start,
                    red_start,
                ]
            )

    print(
        f"\nSummary report saved to:"
        f"\n{filepath}"
    )

    return filepath


# =============================================================================
# PRINT FINAL SUMMARY
# =============================================================================

def print_final_summary(results):
    """
    Print a compact final report.
    """

    print("\n\n")
    print("=" * 100)
    print("FIREFLY AI - FOUR-MONTH CANDLE REPORT")
    print("=" * 100)

    print(
        f"Period: "
        f"{START_DATE.strftime('%Y-%m-%d')} "
        f"to "
        f"{(END_DATE_EXCLUSIVE - timedelta(seconds=1)).strftime('%Y-%m-%d')}"
    )

    print("Timeframe: 1 minute")
    print(
        f"Streak trigger: "
        f"{STREAK_LENGTH_REQUIRED} consecutive candles"
    )

    print("=" * 100)

    header = (
        f"{'PAIR':<20}"
        f"{'CANDLES':>12}"
        f"{'GREEN':>10}"
        f"{'RED':>10}"
        f"{'DOJI':>10}"
        f"{'MAX GREEN':>12}"
        f"{'MAX RED':>10}"
        f"{'GREEN >=7':>12}"
        f"{'RED >=7':>10}"
    )

    print(header)
    print("-" * 100)

    for result in results:

        stats = result["stats"]

        print(
            f"{result['display_name']:<20}"
            f"{stats['total_candles']:>12,}"
            f"{stats['green_candles']:>10,}"
            f"{stats['red_candles']:>10,}"
            f"{stats['doji_candles']:>10,}"
            f"{stats['longest_green']:>12}"
            f"{stats['longest_red']:>10}"
            f"{stats['green_streaks_7_plus']:>12}"
            f"{stats['red_streaks_7_plus']:>10}"
        )

    print("=" * 100)

    print("\nFirefly strategy interpretation:")
    print(
        f"  {STREAK_LENGTH_REQUIRED} GREEN candles "
        f"-> SELL / LOWER signal"
    )

    print(
        f"  {STREAK_LENGTH_REQUIRED} RED candles "
        f"-> BUY / HIGHER signal"
    )

    print("\n")


# =============================================================================
# MAIN
# =============================================================================

def main():

    print("=" * 80)
    print("FIREFLY AI - FOUR-MONTH CANDLE REPORT")
    print("=" * 80)

    print(
        "\nReport period:"
        f"\nFrom: {START_DATE.strftime('%Y-%m-%d %H:%M:%S WAT')}"
        f"\nTo:   {(END_DATE_EXCLUSIVE - timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M:%S WAT')}"
    )

    expected_candles = int(
        (
            datetime_to_timestamp(END_DATE_EXCLUSIVE)
            - datetime_to_timestamp(START_DATE)
        )
        / TIMEFRAME
    )

    print(
        f"\nExpected one-minute candles per pair: "
        f"{expected_candles:,}"
    )

    print(
        f"Number of pairs: {len(CURRENCY_PAIRS)}"
    )

    print(
        f"Approximate maximum total candles: "
        f"{expected_candles * len(CURRENCY_PAIRS):,}"
    )

    # -------------------------------------------------------------------------
    # CONNECT
    # -------------------------------------------------------------------------

    api = connect_to_iq_option()

    # -------------------------------------------------------------------------
    # PRACTICE BALANCE
    # -------------------------------------------------------------------------

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
    # DYNAMIC OTC MAPPINGS
    # -------------------------------------------------------------------------

    if not update_otc_mappings(api):

        print(
            "\nCannot continue because one or more required "
            "OTC assets could not be resolved."
        )

        sys.exit(1)

    # -------------------------------------------------------------------------
    # PROCESS EACH PAIR
    # -------------------------------------------------------------------------

    results = []

    for index, (display_name, asset) in enumerate(
        CURRENCY_PAIRS,
        start=1,
    ):

        print("\n")
        print(
            "#" * 80
        )

        print(
            f"# PAIR {index}/{len(CURRENCY_PAIRS)}"
        )

        print(
            f"# {display_name}"
        )

        print(
            "#" * 80
        )

        # Verify active ID before downloading.
        active_id = stable_api.OP_code.ACTIVES.get(asset)

        if active_id is None:

            print(
                f"SKIPPING {asset}: "
                "no active ID found."
            )

            continue

        print(
            f"Using active ID: {active_id}"
        )

        # ---------------------------------------------------------------------
        # DOWNLOAD
        # ---------------------------------------------------------------------

        candle_map = download_pair(
            api,
            display_name,
            asset,
        )

        if not candle_map:

            print(
                f"\nWARNING: No candle data obtained for {asset}."
            )

            continue

        # ---------------------------------------------------------------------
        # SAVE RAW DATA
        # ---------------------------------------------------------------------

        save_candles(
            asset,
            candle_map,
        )

        # ---------------------------------------------------------------------
        # ANALYZE
        # ---------------------------------------------------------------------

        stats = analyze_streaks(
            candle_map,
        )

        # ---------------------------------------------------------------------
        # PRINT
        # ---------------------------------------------------------------------

        print_analysis(
            display_name,
            asset,
            stats,
        )

        results.append(
            {
                "display_name": display_name,
                "asset": asset,
                "stats": stats,
            }
        )

    # -------------------------------------------------------------------------
    # FINAL REPORT
    # -------------------------------------------------------------------------

    if results:

        print_final_summary(
            results
        )

        save_summary_report(
            results
        )

    else:

        print(
            "\nERROR: No pairs were successfully analyzed."
        )

    print("\n")
    print("=" * 80)
    print("REPORT COMPLETE")
    print("=" * 80)

    print(
        f"\nRaw candle data directory:"
        f"\n{DATA_DIR}"
    )

    print(
        f"\nSummary report directory:"
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
            "Already downloaded data may have been saved "
            "for completed pairs."
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
