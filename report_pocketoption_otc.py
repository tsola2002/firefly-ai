
"""
Firefly AI - Pocket Option Historical OTC Candle Report

Features:
- Fetches historical 1-minute candles with backward pagination.
- Reports candles for a selected date in the configured timezone.
- Calculates maximum GREEN and RED streaks.
- Counts streaks meeting thresholds 7 through 15.
- Stores report runs, raw candles, summaries, and streak frequencies.
- Logs missing data and API errors.
- Never places trades.
"""

import logging
import math
import os
import sqlite3
import time

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from pocketoptionapi import PocketOption


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

PAIRS = [
    "USDJPY_otc",
    "AUDUSD_otc",
    "EURJPY_otc",
    "AUDJPY_otc",
    "GBPJPY_otc",
    "GBPAUD_otc",
    "GBPCAD_otc",
    "CADJPY_otc",
]

CANDLE_PERIOD = int(os.getenv("CANDLE_PERIOD", "60"))
HISTORY_OFFSET = int(os.getenv("HISTORY_OFFSET", "2000"))
MIN_STREAK = int(os.getenv("MIN_STREAK", "7"))
MAX_STREAK = int(os.getenv("MAX_STREAK", "15"))

TIMEZONE_NAME = os.getenv("REPORT_TIMEZONE", "Africa/Lagos")
REPORT_DATE = os.getenv("REPORT_DATE", "").strip()

DATABASE_PATH = Path(
    os.getenv(
        "DATABASE_PATH",
        str(BASE_DIR / "firefly_pocketoption_reports.db"),
    )
)

LOG_PATH = BASE_DIR / "report_pocketoption.log"

CONNECT_TIMEOUT_SECONDS = 45
CONNECT_POLL_SECONDS = 0.25
SUBSCRIBE_WAIT_SECONDS = 1.0

# Prevent an accidental endless pagination loop.
MAX_HISTORY_PAGES = 10

# A completed historical day normally contains 1,440 one-minute candles.
EXPECTED_CANDLES_PER_DAY = 1440


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("firefly_report")


# ============================================================
# DATA MODELS
# ============================================================

@dataclass(frozen=True)
class Candle:
    pair: str
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None

    @property
    def color(self) -> str:
        if self.close > self.open:
            return "GREEN"

        if self.close < self.open:
            return "RED"

        return "DOJI"


@dataclass(frozen=True)
class Streak:
    pair: str
    color: str
    length: int
    start_timestamp: int
    end_timestamp: int


# ============================================================
# TIMEZONE AND DATE UTILITIES
# ============================================================

def get_timezone():
    """Load the requested timezone, with a WAT fallback on Windows."""

    try:
        return ZoneInfo(TIMEZONE_NAME)

    except (ZoneInfoNotFoundError, ValueError):
        if TIMEZONE_NAME == "Africa/Lagos":
            logger.warning(
                "Africa/Lagos timezone database unavailable; "
                "using fixed UTC+1 WAT."
            )
            return timezone(timedelta(hours=1), "WAT")

        raise ValueError(
            f"Timezone '{TIMEZONE_NAME}' is unavailable. "
            "Install tzdata or configure a valid timezone."
        )


def get_report_date(tz) -> date:
    if REPORT_DATE:
        try:
            return date.fromisoformat(REPORT_DATE)
        except ValueError as exc:
            raise ValueError(
                "REPORT_DATE must use YYYY-MM-DD format."
            ) from exc

    return datetime.now(tz).date()


def get_local_day_bounds(report_day: date, tz):
    """Return the selected local day's start and exclusive end as epochs."""

    local_start = datetime(
        report_day.year,
        report_day.month,
        report_day.day,
        tzinfo=tz,
    )

    next_day = report_day + timedelta(days=1)

    local_end = datetime(
        next_day.year,
        next_day.month,
        next_day.day,
        tzinfo=tz,
    )

    return int(local_start.timestamp()), int(local_end.timestamp())


def timestamp_to_local(timestamp: int, tz) -> str:
    return datetime.fromtimestamp(timestamp, tz).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def validate_config():
    if CANDLE_PERIOD != 60:
        raise ValueError(
            "This report is configured for one-minute candles. "
            "Set CANDLE_PERIOD=60."
        )

    if HISTORY_OFFSET < 100:
        raise ValueError(
            "HISTORY_OFFSET must be at least 100 candles."
        )

    if MIN_STREAK < 1:
        raise ValueError("MIN_STREAK must be at least 1.")

    if MAX_STREAK < MIN_STREAK:
        raise ValueError(
            "MAX_STREAK must be greater than or equal to MIN_STREAK."
        )


# ============================================================
# DATABASE
# ============================================================

def open_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row

    conn.execute("PRAGMA foreign_keys = ON")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS report_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL,
            timezone TEXT NOT NULL,
            candle_period INTEGER NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            error_message TEXT
        );

        CREATE TABLE IF NOT EXISTS candle_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_run_id INTEGER NOT NULL,
            report_date TEXT NOT NULL,
            pair TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL,
            color TEXT NOT NULL,
            FOREIGN KEY(report_run_id)
                REFERENCES report_runs(id),
            UNIQUE(report_run_id, pair, timestamp)
        );

        CREATE TABLE IF NOT EXISTS streak_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_run_id INTEGER NOT NULL,
            pair TEXT NOT NULL,
            max_green INTEGER NOT NULL,
            green_start INTEGER,
            green_end INTEGER,
            max_red INTEGER NOT NULL,
            red_start INTEGER,
            red_end INTEGER,
            candle_count INTEGER NOT NULL,
            green_count INTEGER NOT NULL,
            red_count INTEGER NOT NULL,
            doji_count INTEGER NOT NULL,
            FOREIGN KEY(report_run_id)
                REFERENCES report_runs(id),
            UNIQUE(report_run_id, pair)
        );

        CREATE TABLE IF NOT EXISTS streak_frequency (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_run_id INTEGER NOT NULL,
            pair TEXT NOT NULL,
            color TEXT NOT NULL,
            threshold INTEGER NOT NULL,
            event_count INTEGER NOT NULL,
            FOREIGN KEY(report_run_id)
                REFERENCES report_runs(id),
            UNIQUE(
                report_run_id,
                pair,
                color,
                threshold
            )
        );

        CREATE INDEX IF NOT EXISTS idx_candle_pair_time
            ON candle_data(pair, timestamp);

        CREATE INDEX IF NOT EXISTS idx_streak_run_pair
            ON streak_summary(report_run_id, pair);
        """
    )

    conn.commit()
    return conn


def create_report_run(conn, report_day: date) -> int:
    started_at = datetime.now(timezone.utc).isoformat()

    cursor = conn.execute(
        """
        INSERT INTO report_runs (
            report_date,
            timezone,
            candle_period,
            started_at,
            status
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            report_day.isoformat(),
            TIMEZONE_NAME,
            CANDLE_PERIOD,
            started_at,
            "RUNNING",
        ),
    )

    conn.commit()
    return cursor.lastrowid


def finish_report_run(
    conn,
    run_id: int,
    status: str,
    error_message: str | None = None,
):
    conn.execute(
        """
        UPDATE report_runs
        SET completed_at = ?, status = ?, error_message = ?
        WHERE id = ?
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            status,
            error_message,
            run_id,
        ),
    )

    conn.commit()


def save_candles(conn, run_id: int, report_day: date, candles):
    rows = [
        (
            run_id,
            report_day.isoformat(),
            candle.pair,
            candle.timestamp,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
            candle.color,
        )
        for candle in candles
    ]

    conn.executemany(
        """
        INSERT OR IGNORE INTO candle_data (
            report_run_id,
            report_date,
            pair,
            timestamp,
            open,
            high,
            low,
            close,
            volume,
            color
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )

    conn.commit()


def save_pair_report(
    conn,
    run_id: int,
    pair: str,
    candles,
    streaks,
):
    colors = Counter(candle.color for candle in candles)

    green_streaks = [
        streak for streak in streaks if streak.color == "GREEN"
    ]

    red_streaks = [
        streak for streak in streaks if streak.color == "RED"
    ]

    max_green = max(green_streaks, key=lambda s: s.length, default=None)
    max_red = max(red_streaks, key=lambda s: s.length, default=None)

    conn.execute(
        """
        INSERT OR REPLACE INTO streak_summary (
            report_run_id,
            pair,
            max_green,
            green_start,
            green_end,
            max_red,
            red_start,
            red_end,
            candle_count,
            green_count,
            red_count,
            doji_count
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            pair,
            max_green.length if max_green else 0,
            max_green.start_timestamp if max_green else None,
            max_green.end_timestamp if max_green else None,
            max_red.length if max_red else 0,
            max_red.start_timestamp if max_red else None,
            max_red.end_timestamp if max_red else None,
            len(candles),
            colors["GREEN"],
            colors["RED"],
            colors["DOJI"],
        ),
    )

    conn.execute(
        """
        DELETE FROM streak_frequency
        WHERE report_run_id = ? AND pair = ?
        """,
        (run_id, pair),
    )

    frequency_rows = []

    for color in ("GREEN", "RED"):
        color_streaks = [
            streak.length
            for streak in streaks
            if streak.color == color
        ]

        for threshold in range(MIN_STREAK, MAX_STREAK + 1):
            count = sum(
                length >= threshold for length in color_streaks
            )

            frequency_rows.append(
                (
                    run_id,
                    pair,
                    color,
                    threshold,
                    count,
                )
            )

    conn.executemany(
        """
        INSERT INTO streak_frequency (
            report_run_id,
            pair,
            color,
            threshold,
            event_count
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        frequency_rows,
    )

    conn.commit()


# ============================================================
# POCKET OPTION API HELPERS
# ============================================================

def connect_api():
    """Connect using the SSID stored in the local .env file."""

    ssid = os.getenv("PO_SSID", "").strip()

    if not ssid:
        raise RuntimeError(
            "PO_SSID is missing. Add your Pocket Option session "
            "SSID to the local .env file."
        )

    api = PocketOption(ssid)

    logger.info("Connecting to Pocket Option...")

    result = api.connect()

    if isinstance(result, tuple):
        connected = bool(result[0])
        error = result[1] if len(result) > 1 else None
    else:
        connected = bool(result)
        error = None

    if not connected:
        raise RuntimeError(
            f"Pocket Option connection failed: {error}"
        )

    deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS

    while time.monotonic() < deadline:
        try:
            connected_now = api.check_connect()
        except Exception:
            connected_now = True

        try:
            synced = api.api.time_sync.is_synced()
        except Exception:
            synced = False

        if connected_now and synced:
            logger.info("Connected; server time is synchronized.")
            return api

        time.sleep(CONNECT_POLL_SECONDS)

    try:
        api.disconnect_websocket(timeout=12.0)
    except Exception:
        pass

    raise TimeoutError(
        "Connection was established, but server time did not "
        "synchronize before the timeout."
    )


def get_server_times(api):
    """
    Return (UTC server timestamp, native server timestamp).

    The native timestamp is used for API history requests.
    """

    utc_timestamp = float(api.get_server_timestamp())

    native_timestamp = float(
        api.api.time_sync.get_server_native_time()
    )

    native_offset = native_timestamp - utc_timestamp

    return utc_timestamp, native_timestamp, native_offset


def normalize_raw_timestamp(value: Any) -> int:
    """Convert a candle timestamp to integer Unix seconds."""

    timestamp = float(value)

    # Handle millisecond timestamps if the API returns them.
    if timestamp > 100_000_000_000:
        timestamp /= 1000

    return int(timestamp)


def get_assets_safely(api):
    try:
        assets = api.get_assets()

        if isinstance(assets, dict):
            return assets

        logger.warning(
            "Unexpected asset response type: %s",
            type(assets).__name__,
        )

    except Exception:
        logger.exception("Unable to retrieve the asset catalogue.")

    return {}


def find_asset_info(assets, pair):
    """Match a symbol without relying on capitalization."""

    target = pair.casefold()

    for symbol, info in assets.items():
        if str(symbol).casefold() == target:
            return symbol, info

    return None, None


# ============================================================
# HISTORICAL CANDLE RETRIEVAL
# ============================================================

def fetch_pair_candles(api, pair, report_day, tz, assets):
    """
    Fetch historical candles for a local calendar day.

    Pages backward using the earliest timestamp returned by each
    request. Stops when the day is covered, the API returns no
    new candles, or MAX_HISTORY_PAGES is reached.
    """

    local_start, local_end = get_local_day_bounds(report_day, tz)

    utc_now, _, native_offset = get_server_times(api)

    # Do not request future candles when reporting the current day.
    effective_end = min(local_end, int(utc_now))

    if effective_end <= local_start:
        logger.warning(
            "%s: the requested day has not started yet.", pair
        )
        return []

    asset_symbol, asset_info = find_asset_info(assets, pair)

    if asset_info is None:
        logger.warning(
            "%s was not found in the asset catalogue. "
            "Attempting the history request anyway.",
            pair,
        )
    else:
        logger.info(
            "%s asset check: symbol=%s, available=%s, id=%s",
            pair,
            asset_symbol,
            asset_info.get("is_available"),
            asset_info.get("id"),
        )

    # Subscribe first, but do not assume this guarantees historical data.
    try:
        subscribed = api.subscribe(pair, period=CANDLE_PERIOD)
        logger.info("%s subscription result: %s", pair, subscribed)
    except Exception:
        logger.exception("%s subscription failed.", pair)

    time.sleep(SUBSCRIBE_WAIT_SECONDS)

    # Convert the end boundary to the API's native time basis.
    cursor_native = int(effective_end + native_offset)

    collected = {}
    previous_page_oldest = None

    for page_number in range(1, MAX_HISTORY_PAGES + 1):
        logger.info(
            "%s: requesting history page %d, offset=%d",
            pair,
            page_number,
            HISTORY_OFFSET,
        )

        try:
            raw_candles = api.get_historical_candles(
                pair,
                period=CANDLE_PERIOD,
                start_time=cursor_native,
                offset=HISTORY_OFFSET,
                count_request=1,
            )
        except Exception:
            logger.exception(
                "%s: historical candle request failed on page %d.",
                pair,
                page_number,
            )
            break

        if not raw_candles:
            logger.warning(
                "%s: API returned no candles on page %d.",
                pair,
                page_number,
            )
            break

        valid_page_timestamps = []

        for raw in raw_candles:
            try:
                raw_timestamp = normalize_raw_timestamp(raw["time"])

                # Convert server-native time back to UTC epoch seconds.
                timestamp = int(round(raw_timestamp - native_offset))

                valid_page_timestamps.append(raw_timestamp)

                # Only keep candles within the requested local day.
                if not (local_start <= timestamp < effective_end):
                    continue

                candle = Candle(
                    pair=pair,
                    timestamp=timestamp,
                    open=float(raw["open"]),
                    high=float(raw["high"]),
                    low=float(raw["low"]),
                    close=float(raw["close"]),
                    volume=(
                        float(raw["volume"])
                        if raw.get("volume") is not None
                        else None
                    ),
                )

                # Timestamp key removes duplicate candles.
                collected[timestamp] = candle

            except (KeyError, TypeError, ValueError, OverflowError):
                logger.warning(
                    "%s: skipping malformed candle: %r",
                    pair,
                    raw,
                )

        if not valid_page_timestamps:
            logger.warning(
                "%s: page %d contained no valid timestamps.",
                pair,
                page_number,
            )
            break

        page_oldest = min(valid_page_timestamps)

        # Stop if the page cannot move the cursor backwards.
        if (
            previous_page_oldest is not None
            and page_oldest >= previous_page_oldest
        ):
            logger.warning(
                "%s: pagination stopped because the oldest "
                "timestamp did not move backwards.",
                pair,
            )
            break

        previous_page_oldest = page_oldest

        oldest_utc = int(round(page_oldest - native_offset))

        if oldest_utc <= local_start:
            logger.info(
                "%s: reached the start of the report day.",
                pair,
            )
            break

        # The next query should end just before this page's oldest candle.
        cursor_native = int(page_oldest - CANDLE_PERIOD)

    candles = sorted(
        collected.values(),
        key=lambda candle: candle.timestamp,
    )

    if not candles:
        logger.warning(
            "%s: no candles fell within %s in timezone %s.",
            pair,
            report_day.isoformat(),
            TIMEZONE_NAME,
        )
        return []

    first_time = timestamp_to_local(candles[0].timestamp, tz)
    last_time = timestamp_to_local(candles[-1].timestamp, tz)

    expected = max(
        0,
        math.ceil(
            (effective_end - local_start) / CANDLE_PERIOD
        ),
    )

    missing_minutes = max(0, expected - len(candles))

    logger.info(
        "%s: received %d unique candles; first=%s; last=%s; "
        "expected approximately %d; missing=%d",
        pair,
        len(candles),
        first_time,
        last_time,
        expected,
        missing_minutes,
    )

    if len(candles) < expected:
        logger.warning(
            "%s: incomplete candle coverage. This may be due to "
            "API history limits, unavailable history, or gaps.",
            pair,
        )

    return candles


# ============================================================
# STREAK ANALYSIS
# ============================================================

def identify_streaks(candles):
    """
    A streak ends when:
    - candle color changes;
    - a doji appears; or
    - the timestamp gap is not exactly one candle period.
    """

    if not candles:
        return []

    streaks = []

    current_color = None
    current_start = None
    current_end = None
    current_length = 0

    def close_streak():
        if current_color in ("GREEN", "RED") and current_length > 0:
            streaks.append(
                Streak(
                    pair=candles[0].pair,
                    color=current_color,
                    length=current_length,
                    start_timestamp=current_start,
                    end_timestamp=current_end,
                )
            )

    for candle in candles:
        continuous = (
            current_length > 0
            and current_end is not None
            and candle.timestamp - current_end == CANDLE_PERIOD
        )

        if (
            candle.color not in ("GREEN", "RED")
            or candle.color != current_color
            or not continuous
        ):
            close_streak()

            current_color = None
            current_start = None
            current_end = None
            current_length = 0

            if candle.color not in ("GREEN", "RED"):
                continue

            current_color = candle.color
            current_start = candle.timestamp
            current_end = candle.timestamp
            current_length = 1

        else:
            current_end = candle.timestamp
            current_length += 1

    close_streak()

    return streaks


def summarize_pair(candles, streaks):
    green_streaks = [
        streak for streak in streaks if streak.color == "GREEN"
    ]

    red_streaks = [
        streak for streak in streaks if streak.color == "RED"
    ]

    counts = Counter(candle.color for candle in candles)

    return {
        "candle_count": len(candles),
        "green_count": counts["GREEN"],
        "red_count": counts["RED"],
        "doji_count": counts["DOJI"],
        "max_green": max(
            green_streaks, key=lambda s: s.length, default=None
        ),
        "max_red": max(
            red_streaks, key=lambda s: s.length, default=None
        ),
        "green_streaks": green_streaks,
        "red_streaks": red_streaks,
    }


# ============================================================
# CONSOLE REPORTING
# ============================================================

def print_streak_details(title, streak, tz):
    if streak is None:
        print(f"{title}: No streak found")
        return

    print(
        f"{title}: {streak.length} candles | "
        f"Start: {timestamp_to_local(streak.start_timestamp, tz)} | "
        f"End: {timestamp_to_local(streak.end_timestamp, tz)}"
    )


def print_pair_report(pair, candles, streaks, tz):
    summary = summarize_pair(candles, streaks)

    print("\n" + "=" * 76)
    print(f"PAIR: {pair}")
    print("=" * 76)

    print(f"Total candles: {summary['candle_count']}")
    print(f"GREEN candles: {summary['green_count']}")
    print(f"RED candles:   {summary['red_count']}")
    print(f"DOJI candles:  {summary['doji_count']}")

    print_streak_details(
        "Maximum GREEN streak", summary["max_green"], tz
    )
    print_streak_details(
        "Maximum RED streak", summary["max_red"], tz
    )

    print("\nSTREAK FREQUENCY")
    print("-" * 48)
    print(f"{'Threshold':<15}{'GREEN':<15}{'RED':<15}")

    for threshold in range(MIN_STREAK, MAX_STREAK + 1):
        green_count = sum(
            streak.length >= threshold
            for streak in summary["green_streaks"]
        )

        red_count = sum(
            streak.length >= threshold
            for streak in summary["red_streaks"]
        )

        print(
            f"{threshold:<15}{green_count:<15}{red_count:<15}"
        )


def print_overall_summary(results):
    print("\n" + "#" * 76)
    print("OVERALL POCKET OPTION OTC REPORT")
    print("#" * 76)

    print(
        f"{'PAIR':<15}{'CANDLES':<12}"
        f"{'MAX GREEN':<15}{'MAX RED':<15}"
    )

    for pair in PAIRS:
        candles, streaks = results.get(pair, ([], []))
        summary = summarize_pair(candles, streaks)

        if not candles:
            print(f"{pair:<15}{'NO DATA':<12}{'-':<15}{'-':<15}")
            continue

        green = summary["max_green"]
        red = summary["max_red"]

        print(
            f"{pair:<15}"
            f"{len(candles):<12}"
            f"{green.length if green else 0:<15}"
            f"{red.length if red else 0:<15}"
        )

    print("\nReminder: historical streaks are descriptive statistics,")
    print("not predictions or guaranteed trading signals.")


# ============================================================
# MAIN
# ============================================================

def main():
    validate_config()

    tz = get_timezone()
    report_day = get_report_date(tz)
    today = datetime.now(tz).date()

    if report_day > today:
        raise ValueError(
            f"REPORT_DATE {report_day} is in the future."
        )

    logger.info("Report date: %s", report_day)
    logger.info("Timezone: %s", TIMEZONE_NAME)
    logger.info("Candle period: %d seconds", CANDLE_PERIOD)
    logger.info("Database: %s", DATABASE_PATH)

    conn = open_database()
    run_id = create_report_run(conn, report_day)

    api = None
    results = {}
    pair_errors = []

    try:
        api = connect_api()
        assets = get_assets_safely(api)

        for pair in PAIRS:
            try:
                candles = fetch_pair_candles(
                    api=api,
                    pair=pair,
                    report_day=report_day,
                    tz=tz,
                    assets=assets,
                )

                if not candles:
                    results[pair] = ([], [])
                    pair_errors.append(
                        f"{pair}: no historical candles returned"
                    )
                    continue

                streaks = identify_streaks(candles)

                save_candles(
                    conn,
                    run_id,
                    report_day,
                    candles,
                )

                save_pair_report(
                    conn,
                    run_id,
                    pair,
                    candles,
                    streaks,
                )

                results[pair] = (candles, streaks)

                print_pair_report(
                    pair,
                    candles,
                    streaks,
                    tz,
                )

            except Exception as exc:
                logger.exception("Failed to process %s.", pair)

                results[pair] = ([], [])
                pair_errors.append(f"{pair}: {exc}")

        print_overall_summary(results)

        successful_pairs = sum(
            1
            for candles, _ in results.values()
            if candles
        )

        if successful_pairs == len(PAIRS):
            status = "COMPLETED"
        elif successful_pairs > 0:
            status = "PARTIAL"
        else:
            status = "FAILED"

        error_message = (
            "; ".join(pair_errors)[:4000]
            if pair_errors
            else None
        )

        finish_report_run(
            conn,
            run_id,
            status,
            error_message,
        )

        print("\n" + "-" * 76)
        print(f"Report status: {status}")
        print(f"Pairs with data: {successful_pairs}/{len(PAIRS)}")
        print(f"Report run ID: {run_id}")
        print(f"Database: {DATABASE_PATH}")
        print(f"Log file: {LOG_PATH}")
        print("-" * 76)

    except KeyboardInterrupt:
        logger.warning("Report interrupted by user.")

        finish_report_run(
            conn,
            run_id,
            "INTERRUPTED",
            "Keyboard interrupt",
        )

    except Exception as exc:
        logger.exception("Report failed.")

        finish_report_run(
            conn,
            run_id,
            "FAILED",
            str(exc)[:4000],
        )

        raise

    finally:
        if api is not None:
            try:
                api.disconnect_websocket(timeout=12.0)
                logger.info("Pocket Option connection closed.")
            except Exception:
                logger.exception(
                    "Error while closing Pocket Option connection."
                )

        conn.close()


if __name__ == "__main__":
    main()