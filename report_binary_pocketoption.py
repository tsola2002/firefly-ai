
"""
Firefly AI - Pocket Option Historical Binary Candle Report
===========================================================

Features:
- Historical 1-minute candles for eight binary currency pairs.
- Nigeria-local report date (Africa/Lagos).
- Maximum GREEN and RED consecutive-candle streaks.
- Streak frequency for thresholds 7 through 15.
- SQLite persistence for report runs, candles, summaries, and frequencies.
- Historical candle pagination.
- Logging and terminal report output.
- Reporting only; no trades are placed.
"""

import logging
import os
import sqlite3
import time

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from pocketoptionapi import PocketOption


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

PO_SSID = os.getenv("PO_SSID")

PAIRS = [
    "USDJPY",
    "AUDUSD",
    "EURJPY",
    "AUDJPY",
    "GBPJPY",
    "GBPAUD",
    "GBPCAD",
    "CADJPY",
]

CANDLE_PERIOD = 60
HISTORY_OFFSET = 2000
MAX_HISTORY_PAGES = 10

MIN_STREAK = 7
MAX_STREAK = 15

TIMEZONE_NAME = "Africa/Lagos"

REPORT_DATE_ENV = os.getenv("REPORT_DATE")

DB_PATH = BASE_DIR / "firefly_pocketoption_binary_reports.db"
LOG_PATH = BASE_DIR / "report_binary_pocketoption.log"


# ============================================================
# TIMEZONE AND REPORT DATE
# ============================================================

try:
    REPORT_TIMEZONE = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    REPORT_TIMEZONE = timezone(timedelta(hours=1))

TIMEZONE_LABEL = TIMEZONE_NAME


def get_report_date() -> date:
    """Return the requested report date or today's Nigeria-local date."""
    if REPORT_DATE_ENV:
        return date.fromisoformat(REPORT_DATE_ENV)

    return datetime.now(REPORT_TIMEZONE).date()


def get_day_bounds(report_date: date) -> tuple[datetime, datetime]:
    """Return the start and end of the report date in the report timezone."""
    start = datetime.combine(
        report_date,
        datetime.min.time(),
        tzinfo=REPORT_TIMEZONE,
    )

    end = start + timedelta(days=1)

    return start, end


def to_timestamp(value) -> Optional[int]:
    """Normalize a candle timestamp to Unix seconds."""
    if value is None:
        return None

    try:
        timestamp = float(value)

        # Convert milliseconds to seconds if necessary.
        if timestamp > 10_000_000_000:
            timestamp /= 1000

        return int(timestamp)
    except (TypeError, ValueError, OverflowError):
        return None


def timestamp_to_local(timestamp: int) -> datetime:
    """Convert a Unix timestamp to the report timezone."""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(
        REPORT_TIMEZONE
    )


# ============================================================
# LOGGING
# ============================================================

logger = logging.getLogger("firefly_binary_report")
logger.setLevel(logging.INFO)
logger.handlers.clear()

formatter = logging.Formatter(
    "%(asctime)s | %(levelname)s | %(message)s"
)

file_handler = logging.FileHandler(
    LOG_PATH,
    encoding="utf-8",
)
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class Candle:
    pair: str
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    color: str


@dataclass
class Streak:
    color: str
    length: int
    start_timestamp: int
    end_timestamp: int


# ============================================================
# DATABASE
# ============================================================

class ReportDatabase:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.connection = sqlite3.connect(str(db_path))
        self.connection.row_factory = sqlite3.Row
        self.create_tables()

    def create_tables(self):
        """Create the report database tables."""
        cursor = self.connection.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS report_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date TEXT NOT NULL,
                timezone TEXT NOT NULL,
                candle_period INTEGER NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                error_message TEXT
            )
        """)

        cursor.execute("""
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
                volume REAL NOT NULL,
                color TEXT NOT NULL,
                FOREIGN KEY (report_run_id)
                    REFERENCES report_runs(id),
                UNIQUE (report_run_id, pair, timestamp)
            )
        """)

        cursor.execute("""
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
                FOREIGN KEY (report_run_id)
                    REFERENCES report_runs(id),
                UNIQUE (report_run_id, pair)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS streak_frequency (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_run_id INTEGER NOT NULL,
                pair TEXT NOT NULL,
                color TEXT NOT NULL,
                threshold INTEGER NOT NULL,
                event_count INTEGER NOT NULL,
                FOREIGN KEY (report_run_id)
                    REFERENCES report_runs(id),
                UNIQUE (
                    report_run_id,
                    pair,
                    color,
                    threshold
                )
            )
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_candle_pair_time
            ON candle_data(pair, timestamp)
        """)

        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_streak_run_pair
            ON streak_summary(report_run_id, pair)
        """)

        self.connection.commit()

    def create_run(self, report_date: date) -> int:
        """Create a new report run and return its ID."""
        cursor = self.connection.cursor()

        cursor.execute("""
            INSERT INTO report_runs (
                report_date,
                timezone,
                candle_period,
                started_at,
                status
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            report_date.isoformat(),
            TIMEZONE_LABEL,
            CANDLE_PERIOD,
            datetime.now(REPORT_TIMEZONE).isoformat(),
            "RUNNING",
        ))

        self.connection.commit()
        return cursor.lastrowid

    def save_candles(
        self,
        run_id: int,
        report_date: date,
        candles: list[Candle],
    ):
        """Persist historical candles."""
        cursor = self.connection.cursor()

        cursor.executemany("""
            INSERT OR REPLACE INTO candle_data (
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
        """, [
            (
                run_id,
                report_date.isoformat(),
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
        ])

        self.connection.commit()

    def save_streak_summary(
        self,
        run_id: int,
        pair: str,
        summary: dict,
    ):
        cursor = self.connection.cursor()

        cursor.execute("""
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
        """, (
            run_id,
            pair,
            summary["max_green"],
            summary["green_start"],
            summary["green_end"],
            summary["max_red"],
            summary["red_start"],
            summary["red_end"],
            summary["candle_count"],
            summary["green_count"],
            summary["red_count"],
            summary["doji_count"],
        ))

        self.connection.commit()

    def save_streak_frequencies(
        self,
        run_id: int,
        pair: str,
        frequencies: dict,
    ):
        cursor = self.connection.cursor()

        rows = []

        for color in ("GREEN", "RED"):
            for threshold in range(MIN_STREAK, MAX_STREAK + 1):
                rows.append((
                    run_id,
                    pair,
                    color,
                    threshold,
                    frequencies[color].get(threshold, 0),
                ))

        cursor.executemany("""
            INSERT OR REPLACE INTO streak_frequency (
                report_run_id,
                pair,
                color,
                threshold,
                event_count
            )
            VALUES (?, ?, ?, ?, ?)
        """, rows)

        self.connection.commit()

    def complete_run(
        self,
        run_id: int,
        status: str,
        error_message: Optional[str] = None,
    ):
        cursor = self.connection.cursor()

        cursor.execute("""
            UPDATE report_runs
            SET completed_at = ?,
                status = ?,
                error_message = ?
            WHERE id = ?
        """, (
            datetime.now(REPORT_TIMEZONE).isoformat(),
            status,
            error_message,
            run_id,
        ))

        self.connection.commit()

    def close(self):
        self.connection.close()


# ============================================================
# POCKET OPTION CONNECTION
# ============================================================

def connect_to_pocket_option():
    """Connect to Pocket Option using PO_SSID from the environment."""
    if not PO_SSID:
        raise RuntimeError(
            "PO_SSID was not found. Add PO_SSID to your .env file."
        )

    logger.info("Connecting to Pocket Option...")

    api = PocketOption(PO_SSID)
    api.connect()

    deadline = time.time() + 30

    while time.time() < deadline:
        if api.check_connect():
            logger.info("Pocket Option connection established.")
            return api

        time.sleep(1)

    raise ConnectionError(
        "Could not establish a Pocket Option connection."
    )


# ============================================================
# ASSET VALIDATION
# ============================================================

def validate_asset(api, pair: str) -> bool:
    """Check whether a requested binary currency pair is available."""
    try:
        assets = api.get_assets()

        if isinstance(assets, dict):
            available_assets = list(assets.keys())
        elif isinstance(assets, (list, tuple, set)):
            available_assets = list(assets)
        else:
            available_assets = []

        normalized_assets = {
            str(asset).strip().upper().replace("/", "")
            for asset in available_assets
        }

        normalized_pair = pair.upper().replace("/", "")

        matched_symbol = next(
            (
                asset
                for asset in available_assets
                if str(asset).strip().upper().replace("/", "")
                == normalized_pair
            ),
            None,
        )

        available = normalized_pair in normalized_assets

        logger.info(
            "%s asset check: matched symbol=%s, available=%s",
            pair,
            matched_symbol,
            available,
        )

        return available

    except Exception as exc:
        logger.warning(
            "%s asset validation failed: %s",
            pair,
            exc,
        )
        return False


# ============================================================
# HISTORICAL CANDLE RETRIEVAL
# ============================================================

def extract_candle_value(candle: dict, *keys, default=0):
    """Get a candle field using the possible API field names."""
    for key in keys:
        if key in candle and candle[key] is not None:
            return candle[key]

    return default


def normalize_candle(pair: str, raw: dict) -> Optional[Candle]:
    """Convert a raw Pocket Option candle into a Candle object."""
    timestamp = to_timestamp(
        extract_candle_value(
            raw,
            "timestamp",
            "time",
            "at",
            "from",
            default=None,
        )
    )

    if timestamp is None:
        return None

    try:
        open_price = float(
            extract_candle_value(raw, "open", "o")
        )
        high_price = float(
            extract_candle_value(raw, "high", "max", "h")
        )
        low_price = float(
            extract_candle_value(raw, "low", "min", "l")
        )
        close_price = float(
            extract_candle_value(raw, "close", "c")
        )
        volume = float(
            extract_candle_value(raw, "volume", "v", default=0)
        )
    except (TypeError, ValueError):
        return None

    if close_price > open_price:
        color = "GREEN"
    elif close_price < open_price:
        color = "RED"
    else:
        color = "DOJI"

    return Candle(
        pair=pair,
        timestamp=timestamp,
        open=open_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=volume,
        color=color,
    )


def fetch_historical_candles(
    api,
    pair: str,
    report_date: date,
) -> list[Candle]:
    """Fetch historical one-minute candles for the requested date."""
    start_local, end_local = get_day_bounds(report_date)

    start_timestamp = int(start_local.timestamp())
    end_timestamp = int(end_local.timestamp())

    logger.info(
        "%s: fetching historical candles for %s",
        pair,
        report_date.isoformat(),
    )

    api.subscribe(pair, period=CANDLE_PERIOD)

    logger.info(
        "%s subscription requested: period=%s",
        pair,
        CANDLE_PERIOD,
    )

    cursor_native = end_timestamp
    collected = {}

    for page in range(MAX_HISTORY_PAGES):
        try:
            raw_candles = api.get_historical_candles(
                pair,
                period=CANDLE_PERIOD,
                start_time=cursor_native,
                offset=HISTORY_OFFSET,
                count_request=1,
            )
        except Exception as exc:
            logger.warning(
                "%s history page %s failed: %s",
                pair,
                page + 1,
                exc,
            )
            break

        if not raw_candles:
            logger.info(
                "%s: no candles returned on page %s",
                pair,
                page + 1,
            )
            break

        if isinstance(raw_candles, dict):
            raw_candles = raw_candles.get(
                "candles",
                raw_candles.get("data", []),
            )

        if not raw_candles:
            break

        page_candles = []

        for raw in raw_candles:
            if not isinstance(raw, dict):
                continue

            candle = normalize_candle(pair, raw)

            if candle is None:
                continue

            if start_timestamp <= candle.timestamp < end_timestamp:
                collected[candle.timestamp] = candle

            page_candles.append(candle)

        if not page_candles:
            break

        oldest_timestamp = min(
            candle.timestamp for candle in page_candles
        )

        logger.info(
            "%s page %s: received=%s, oldest=%s",
            pair,
            page + 1,
            len(page_candles),
            timestamp_to_local(oldest_timestamp).isoformat(),
        )

        if oldest_timestamp <= start_timestamp:
            break

        next_cursor = oldest_timestamp - 1

        if next_cursor >= cursor_native:
            break

        cursor_native = next_cursor

    candles = sorted(
        collected.values(),
        key=lambda candle: candle.timestamp,
    )

    logger.info(
        "%s: collected %s candles for %s",
        pair,
        len(candles),
        report_date.isoformat(),
    )

    return candles


# ============================================================
# STREAK ANALYSIS
# ============================================================

def analyze_streaks(candles: list[Candle]) -> tuple[dict, dict]:
    """Calculate maximum streaks and streak frequencies."""
    ordered_candles = sorted(
        candles,
        key=lambda candle: candle.timestamp,
    )

    green_count = sum(
        candle.color == "GREEN" for candle in ordered_candles
    )
    red_count = sum(
        candle.color == "RED" for candle in ordered_candles
    )
    doji_count = sum(
        candle.color == "DOJI" for candle in ordered_candles
    )

    max_green = 0
    max_red = 0

    green_start = None
    green_end = None
    red_start = None
    red_end = None

    current_color = None
    current_length = 0
    current_start = None

    frequencies = {
        "GREEN": {
            threshold: 0
            for threshold in range(MIN_STREAK, MAX_STREAK + 1)
        },
        "RED": {
            threshold: 0
            for threshold in range(MIN_STREAK, MAX_STREAK + 1)
        },
    }

    def record_streak(color, length, start_timestamp, end_timestamp):
        nonlocal max_green, max_red
        nonlocal green_start, green_end, red_start, red_end

        if color not in ("GREEN", "RED"):
            return

        for threshold in range(MIN_STREAK, MAX_STREAK + 1):
            if length >= threshold:
                frequencies[color][threshold] += 1

        if color == "GREEN" and length > max_green:
            max_green = length
            green_start = start_timestamp
            green_end = end_timestamp

        elif color == "RED" and length > max_red:
            max_red = length
            red_start = start_timestamp
            red_end = end_timestamp

    for candle in ordered_candles:
        if candle.color == current_color:
            current_length += 1
        else:
            if current_color in ("GREEN", "RED") and current_length:
                record_streak(
                    current_color,
                    current_length,
                    current_start,
                    previous_timestamp,
                )

            current_color = candle.color
            current_length = 1
            current_start = candle.timestamp

        previous_timestamp = candle.timestamp

    if current_color in ("GREEN", "RED") and current_length:
        record_streak(
            current_color,
            current_length,
            current_start,
            previous_timestamp,
        )

    summary = {
        "max_green": max_green,
        "green_start": green_start,
        "green_end": green_end,
        "max_red": max_red,
        "red_start": red_start,
        "red_end": red_end,
        "candle_count": len(ordered_candles),
        "green_count": green_count,
        "red_count": red_count,
        "doji_count": doji_count,
    }

    return summary, frequencies


# ============================================================
# REPORT OUTPUT
# ============================================================

def format_timestamp(timestamp: Optional[int]) -> str:
    if timestamp is None:
        return "N/A"

    return timestamp_to_local(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def print_pair_report(
    pair: str,
    summary: dict,
    frequencies: dict,
):
    print()
    print("=" * 72)
    print(f"PAIR: {pair}")
    print("=" * 72)

    print(f"Total candles : {summary['candle_count']}")
    print(f"GREEN candles : {summary['green_count']}")
    print(f"RED candles   : {summary['red_count']}")
    print(f"DOJI candles  : {summary['doji_count']}")

    print()
    print("MAXIMUM CONSECUTIVE STREAKS")
    print("-" * 72)

    print(f"Maximum GREEN streak: {summary['max_green']}")
    print(
        "GREEN streak start : "
        f"{format_timestamp(summary['green_start'])}"
    )
    print(
        "GREEN streak end   : "
        f"{format_timestamp(summary['green_end'])}"
    )

    print()

    print(f"Maximum RED streak  : {summary['max_red']}")
    print(
        "RED streak start   : "
        f"{format_timestamp(summary['red_start'])}"
    )
    print(
        "RED streak end     : "
        f"{format_timestamp(summary['red_end'])}"
    )

    print()
    print(f"STREAK FREQUENCY ({MIN_STREAK}-{MAX_STREAK})")
    print("-" * 72)
    print(f"{'Threshold':<15}{'GREEN':<15}{'RED':<15}")

    for threshold in range(MIN_STREAK, MAX_STREAK + 1):
        green_frequency = frequencies["GREEN"].get(threshold, 0)
        red_frequency = frequencies["RED"].get(threshold, 0)

        print(
            f"{threshold:<15}"
            f"{green_frequency:<15}"
            f"{red_frequency:<15}"
        )


def print_overall_report(
    report_date: date,
    results: dict,
):
    print()
    print("=" * 72)
    print("OVERALL POCKET OPTION BINARY REPORT")
    print("=" * 72)
    print(f"Report date : {report_date.isoformat()}")
    print(f"Timezone    : {TIMEZONE_LABEL}")
    print(f"Period      : {CANDLE_PERIOD} seconds")
    print(f"Pairs       : {len(PAIRS)}")

    for pair in PAIRS:
        if pair not in results:
            print()
            print("=" * 72)
            print(f"PAIR: {pair}")
            print("=" * 72)
            print("No report data available.")
            continue

        summary = results[pair]["summary"]
        frequencies = results[pair]["frequencies"]

        print_pair_report(pair, summary, frequencies)

    print()
    print("=" * 72)
    print("OVERALL SUMMARY")
    print("=" * 72)
    print(
        f"{'Pair':<12}"
        f"{'Candles':<12}"
        f"{'Max GREEN':<14}"
        f"{'Max RED':<12}"
    )

    for pair in PAIRS:
        if pair not in results:
            print(f"{pair:<12}{'N/A':<12}{'N/A':<14}{'N/A':<12}")
            continue

        summary = results[pair]["summary"]

        print(
            f"{pair:<12}"
            f"{summary['candle_count']:<12}"
            f"{summary['max_green']:<14}"
            f"{summary['max_red']:<12}"
        )

    print("=" * 72)


# ============================================================
# MAIN
# ============================================================

def main():
    report_date = get_report_date()
    database = ReportDatabase(DB_PATH)

    run_id = database.create_run(report_date)
    api = None

    results = {}

    logger.info("=" * 72)
    logger.info("Starting Pocket Option binary historical report")
    logger.info("Report date: %s", report_date.isoformat())
    logger.info("Timezone: %s", TIMEZONE_LABEL)
    logger.info("Database: %s", DB_PATH)
    logger.info("=" * 72)

    try:
        api = connect_to_pocket_option()

        for pair in PAIRS:
            logger.info("-" * 60)
            logger.info("Processing %s", pair)

            if not validate_asset(api, pair):
                logger.warning(
                    "%s is not available; skipping pair.",
                    pair,
                )
                continue

            candles = fetch_historical_candles(
                api,
                pair,
                report_date,
            )

            database.save_candles(
                run_id,
                report_date,
                candles,
            )

            summary, frequencies = analyze_streaks(candles)

            database.save_streak_summary(
                run_id,
                pair,
                summary,
            )

            database.save_streak_frequencies(
                run_id,
                pair,
                frequencies,
            )

            results[pair] = {
                "summary": summary,
                "frequencies": frequencies,
            }

            logger.info(
                "%s complete: candles=%s, max GREEN=%s, max RED=%s",
                pair,
                summary["candle_count"],
                summary["max_green"],
                summary["max_red"],
            )

        print_overall_report(report_date, results)
        database.complete_run(run_id, "COMPLETED")

        logger.info("Binary report completed successfully.")

    except Exception as exc:
        logger.exception("Binary report failed: %s", exc)
        database.complete_run(
            run_id,
            "FAILED",
            str(exc),
        )
        raise

    finally:
        if api is not None:
            try:
                api.disconnect_websocket(timeout=12.0)
                logger.info("Pocket Option connection closed.")
            except Exception as exc:
                logger.warning(
                    "Error closing Pocket Option connection: %s",
                    exc,
                )

        database.close()
        logger.info("Database connection closed.")


if __name__ == "__main__":
    main()
