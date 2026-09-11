from __future__ import annotations

import logging
import os
import signal
import sqlite3
import sys
import time

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Event, Lock
from typing import Any, Optional

from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option


# =============================================================================
# FIREFLY AI - CONFIGURATION
# =============================================================================

PROJECT_ROOT = Path(__file__).resolve().parent

DATABASE_FILE = PROJECT_ROOT / "firefly.db"
LOG_DIRECTORY = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIRECTORY / "candle_collector.log"
ENV_FILE = PROJECT_ROOT / ".env"

MARKET_TYPE = "BINARY"

TIMEFRAME_SECONDS = 60

# Number of candles requested when initially populating the database.
INITIAL_CANDLES = 100

# Number of recent candles requested during normal polling.
LATEST_CANDLES = 10

# How frequently Firefly checks for new completed candles.
POLL_INTERVAL_SECONDS = 5

# -------------------------------------------------------------------------
# CONNECTION SETTINGS
# -------------------------------------------------------------------------

INITIAL_CONNECT_RETRY_DELAY_SECONDS = 10
MAX_CONNECT_RETRY_DELAY_SECONDS = 60

# -------------------------------------------------------------------------
# MAPPING SETTINGS
# -------------------------------------------------------------------------

# update_ACTIVES_OPCODE() can block indefinitely in iqoptionapi.
# We therefore execute it with a timeout.
MAPPING_TIMEOUT_SECONDS = 15

# How long to wait before trying a fresh mapping refresh.
MAPPING_REFRESH_INTERVAL_SECONDS = 15 * 60

# -------------------------------------------------------------------------
# CANDLE REQUEST SETTINGS
# -------------------------------------------------------------------------

CANDLE_REQUEST_TIMEOUT_SECONDS = 20
CANDLE_RETRY_DELAY_SECONDS = 5
MAX_CANDLE_RETRIES = 3

# -------------------------------------------------------------------------
# RECONNECT SETTINGS
# -------------------------------------------------------------------------

RECONNECT_DELAY_SECONDS = 10
MAX_RECONNECT_DELAY_SECONDS = 60

# -------------------------------------------------------------------------
# REAL BINARY FOREX PAIRS ONLY
# -------------------------------------------------------------------------

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

# -------------------------------------------------------------------------
# VERIFIED IQ OPTION BINARY ACTIVE IDs
#
# These were obtained from your successful Binary mapping diagnostic.
#
# They are used as a controlled fallback if update_ACTIVES_OPCODE() hangs
# or fails.
#
# IMPORTANT:
# These are REAL Binary mappings.
# No OTC symbols are used.
# -------------------------------------------------------------------------

KNOWN_BINARY_ACTIVE_IDS = {
    "USDJPY": 6,
    "AUDUSD": 99,
    "EURJPY": 4,
    "AUDJPY": 101,
    "GBPJPY": 3,
    "GBPAUD": 104,
    "GBPCAD": 102,
    "CADJPY": 945,
}


# =============================================================================
# TIMEZONE
# =============================================================================

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        WAT = ZoneInfo("Africa/Lagos")
        TIMEZONE_NAME = "Africa/Lagos"
    except ZoneInfoNotFoundError:
        WAT = timezone(timedelta(hours=1))
        TIMEZONE_NAME = "UTC+01:00 fallback"

except ImportError:
    WAT = timezone(timedelta(hours=1))
    TIMEZONE_NAME = "UTC+01:00 fallback"


# =============================================================================
# LOGGING
# =============================================================================

LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            LOG_FILE,
            mode="a",
            encoding="utf-8",
        ),
    ],
)

logger = logging.getLogger("firefly_candle_collector")


# =============================================================================
# DATA MODEL
# =============================================================================

@dataclass
class Candle:
    pair: str
    asset: str
    timestamp: int
    timestamp_wat: str
    open: float
    high: float
    low: float
    close: float
    volume: Optional[float]
    candle_color: str


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def now_timestamp() -> int:
    return int(time.time())


def timestamp_to_wat(timestamp: int) -> str:
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return dt.astimezone(WAT).strftime("%Y-%m-%d %H:%M:%S")


def candle_color(open_price: float, close_price: float) -> str:
    if close_price > open_price:
        return "GREEN"

    if close_price < open_price:
        return "RED"

    return "DOJI"


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None

        return float(value)

    except (TypeError, ValueError):
        return None


def safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None

        return int(float(value))

    except (TypeError, ValueError):
        return None


# =============================================================================
# SQLITE DATABASE
# =============================================================================

class CandleDatabase:

    def __init__(self, database_file: Path):
        self.database_file = database_file
        self.lock = Lock()

        self.connection = sqlite3.connect(
            str(database_file),
            check_same_thread=False,
            timeout=30,
        )

        self.connection.row_factory = sqlite3.Row

        self._configure_database()
        self._create_tables()

        logger.info(
            "SQLite database ready: %s",
            database_file,
        )

    # -------------------------------------------------------------------------
    # DATABASE CONFIGURATION
    # -------------------------------------------------------------------------

    def _configure_database(self) -> None:

        cursor = self.connection.cursor()

        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")

        self.connection.commit()

    # -------------------------------------------------------------------------
    # TABLES
    # -------------------------------------------------------------------------

    def _create_tables(self) -> None:

        with self.lock:

            cursor = self.connection.cursor()

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    pair TEXT NOT NULL,

                    asset TEXT NOT NULL,

                    market TEXT NOT NULL DEFAULT 'BINARY',

                    timeframe_seconds INTEGER NOT NULL DEFAULT 60,

                    timestamp INTEGER NOT NULL,

                    timestamp_wat TEXT NOT NULL,

                    open REAL NOT NULL,

                    high REAL NOT NULL,

                    low REAL NOT NULL,

                    close REAL NOT NULL,

                    volume REAL,

                    candle_color TEXT NOT NULL,

                    created_at INTEGER NOT NULL,

                    updated_at INTEGER NOT NULL,

                    UNIQUE(
                        asset,
                        market,
                        timeframe_seconds,
                        timestamp
                    )
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS collector_state (
                    asset TEXT NOT NULL,

                    market TEXT NOT NULL,

                    timeframe_seconds INTEGER NOT NULL,

                    last_candle_timestamp INTEGER,

                    last_candle_timestamp_wat TEXT,

                    candles_collected INTEGER NOT NULL DEFAULT 0,

                    last_success_at INTEGER,

                    last_error_at INTEGER,

                    last_error TEXT,

                    PRIMARY KEY(
                        asset,
                        market,
                        timeframe_seconds
                    )
                )
                """
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS collector_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    event_time INTEGER NOT NULL,

                    event_time_wat TEXT NOT NULL,

                    event_type TEXT NOT NULL,

                    asset TEXT,

                    message TEXT
                )
                """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_candles_pair_timestamp
                ON candles(pair, timestamp)
                """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_candles_asset_timestamp
                ON candles(asset, timestamp)
                """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_candles_color
                ON candles(pair, candle_color)
                """
            )

            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_events_time
                ON collector_events(event_time)
                """
            )

            self.connection.commit()

    # -------------------------------------------------------------------------
    # INSERT CANDLE
    # -------------------------------------------------------------------------

    def insert_candle(self, candle: Candle) -> bool:

        current_time = now_timestamp()

        with self.lock:

            cursor = self.connection.cursor()

            cursor.execute(
                """
                INSERT OR IGNORE INTO candles (
                    pair,
                    asset,
                    market,
                    timeframe_seconds,
                    timestamp,
                    timestamp_wat,
                    open,
                    high,
                    low,
                    close,
                    volume,
                    candle_color,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candle.pair,
                    candle.asset,
                    MARKET_TYPE,
                    TIMEFRAME_SECONDS,
                    candle.timestamp,
                    candle.timestamp_wat,
                    candle.open,
                    candle.high,
                    candle.low,
                    candle.close,
                    candle.volume,
                    candle.candle_color,
                    current_time,
                    current_time,
                ),
            )

            inserted = cursor.rowcount > 0

            self.connection.commit()

            return inserted

    # -------------------------------------------------------------------------
    # INSERT MULTIPLE CANDLES
    # -------------------------------------------------------------------------

    def insert_candles(self, candles: list[Candle]) -> int:

        if not candles:
            return 0

        inserted_count = 0
        current_time = now_timestamp()

        with self.lock:

            cursor = self.connection.cursor()

            for candle in candles:

                cursor.execute(
                    """
                    INSERT OR IGNORE INTO candles (
                        pair,
                        asset,
                        market,
                        timeframe_seconds,
                        timestamp,
                        timestamp_wat,
                        open,
                        high,
                        low,
                        close,
                        volume,
                        candle_color,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candle.pair,
                        candle.asset,
                        MARKET_TYPE,
                        TIMEFRAME_SECONDS,
                        candle.timestamp,
                        candle.timestamp_wat,
                        candle.open,
                        candle.high,
                        candle.low,
                        candle.close,
                        candle.volume,
                        candle.candle_color,
                        current_time,
                        current_time,
                    ),
                )

                if cursor.rowcount > 0:
                    inserted_count += 1

            self.connection.commit()

        return inserted_count

    # -------------------------------------------------------------------------
    # UPDATE STATE
    # -------------------------------------------------------------------------

    def update_state(
        self,
        asset: str,
        last_candle_timestamp: Optional[int],
        candles_added: int = 0,
        error: Optional[str] = None,
    ) -> None:

        market = MARKET_TYPE
        timeframe_seconds = TIMEFRAME_SECONDS

        current_time = now_timestamp()

        if error:

            with self.lock:

                self.connection.execute(
                    """
                    INSERT INTO collector_state (
                        asset,
                        market,
                        timeframe_seconds,
                        last_candle_timestamp,
                        last_candle_timestamp_wat,
                        candles_collected,
                        last_success_at,
                        last_error_at,
                        last_error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

                    ON CONFLICT(
                        asset,
                        market,
                        timeframe_seconds
                    )
                    DO UPDATE SET
                        last_error_at = excluded.last_error_at,
                        last_error = excluded.last_error
                    """,
                    (
                        asset,
                        market,
                        timeframe_seconds,
                        last_candle_timestamp,
                        (
                            timestamp_to_wat(last_candle_timestamp)
                            if last_candle_timestamp
                            else None
                        ),
                        candles_added,
                        None,
                        current_time,
                        error,
                    ),
                )

                self.connection.commit()

            return

        with self.lock:

            self.connection.execute(
                """
                INSERT INTO collector_state (
                    asset,
                    market,
                    timeframe_seconds,
                    last_candle_timestamp,
                    last_candle_timestamp_wat,
                    candles_collected,
                    last_success_at,
                    last_error_at,
                    last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT(
                    asset,
                    market,
                    timeframe_seconds
                )
                DO UPDATE SET
                    last_candle_timestamp =
                        COALESCE(
                            excluded.last_candle_timestamp,
                            collector_state.last_candle_timestamp
                        ),

                    last_candle_timestamp_wat =
                        COALESCE(
                            excluded.last_candle_timestamp_wat,
                            collector_state.last_candle_timestamp_wat
                        ),

                    candles_collected =
                        collector_state.candles_collected
                        + excluded.candles_collected,

                    last_success_at =
                        excluded.last_success_at,

                    last_error_at = NULL,

                    last_error = NULL
                """,
                (
                    asset,
                    market,
                    timeframe_seconds,
                    last_candle_timestamp,
                    (
                        timestamp_to_wat(last_candle_timestamp)
                        if last_candle_timestamp
                        else None
                    ),
                    candles_added,
                    current_time,
                    None,
                    None,
                ),
            )

            self.connection.commit()

    # -------------------------------------------------------------------------
    # EVENTS
    # -------------------------------------------------------------------------

    def log_event(
        self,
        event_type: str,
        asset: Optional[str],
        message: str,
    ) -> None:

        current_time = now_timestamp()

        with self.lock:

            self.connection.execute(
                """
                INSERT INTO collector_events (
                    event_time,
                    event_time_wat,
                    event_type,
                    asset,
                    message
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    current_time,
                    timestamp_to_wat(current_time),
                    event_type,
                    asset,
                    message,
                ),
            )

            self.connection.commit()

    # -------------------------------------------------------------------------
    # CLOSE
    # -------------------------------------------------------------------------

    def close(self) -> None:

        with self.lock:

            try:
                self.connection.commit()
                self.connection.close()

                logger.info("SQLite database closed.")

            except Exception as error:

                logger.error(
                    "Error closing SQLite database: %s",
                    error,
                )


# =============================================================================
# IQ OPTION CLIENT
# =============================================================================

class IQOptionClient:

    def __init__(
        self,
        email: str,
        password: str,
    ):

        self.email = email
        self.password = password

        self.api: Optional[IQ_Option] = None

        self.mapping = dict(KNOWN_BINARY_ACTIVE_IDS)

        self.mapping_last_refresh = 0

        self.mapping_lock = Lock()

    # -------------------------------------------------------------------------
    # CREATE API OBJECT
    # -------------------------------------------------------------------------

    def _create_api(self) -> IQ_Option:

        return IQ_Option(
            self.email,
            self.password,
        )

    # -------------------------------------------------------------------------
    # DISCONNECT
    # -------------------------------------------------------------------------

    def disconnect(self) -> None:

        if self.api is None:
            return

        try:

            if hasattr(self.api, "close"):

                self.api.close()

            elif hasattr(self.api, "disconnect"):

                self.api.disconnect()

            else:

                logger.info(
                    "IQ Option API has no explicit "
                    "close/disconnect method."
                )

        except Exception as error:

            logger.warning(
                "Error while disconnecting IQ Option API: %s",
                error,
            )

        finally:

            self.api = None

    # -------------------------------------------------------------------------
    # CONNECT
    # -------------------------------------------------------------------------

    def connect(self) -> bool:

        self.disconnect()

        logger.info("Creating new IQ Option API connection...")

        try:

            self.api = self._create_api()

            result = self.api.connect()

            if isinstance(result, tuple):

                connected = bool(result[0])

                reason = result[1] if len(result) > 1 else None

            else:

                connected = bool(result)
                reason = None

            if not connected:

                logger.error(
                    "IQ Option connection failed. Reason: %r",
                    reason,
                )

                self.disconnect()

                return False

            logger.info("IQ Option connection successful.")

            # -----------------------------------------------------------------
            # Switch to PRACTICE account.
            # -----------------------------------------------------------------

            try:

                self.api.change_balance("PRACTICE")

                balance = self.api.get_balance()

                logger.info(
                    "Practice balance: $%.2f",
                    float(balance),
                )

            except Exception as error:

                logger.warning(
                    "Connected, but could not read practice balance: %s",
                    error,
                )

            return True

        except Exception as error:

            # This catches the JSONDecodeError generated by iqoptionapi
            # after a WinError 10060.
            logger.error(
                "IQ Option connection exception: %s",
                error,
            )

            if "10060" in str(error):

                logger.error(
                    "Windows WinError 10060 detected. "
                    "IQ Option endpoint/network connection timed out."
                )

            if "JSONDecodeError" in type(error).__name__:

                logger.error(
                    "iqoptionapi attempted to parse a non-JSON "
                    "connection failure response."
                )

            self.disconnect()

            return False

    # -------------------------------------------------------------------------
    # CHECK CONNECTION
    # -------------------------------------------------------------------------

    def is_connected(self) -> bool:

        if self.api is None:
            return False

        try:

            return bool(self.api.check_connect())

        except Exception as error:

            logger.warning(
                "Connection check failed: %s",
                error,
            )

            return False

    # -------------------------------------------------------------------------
    # UPDATE BINARY MAPPINGS
    #
    # IMPORTANT:
    #
    # update_ACTIVES_OPCODE() can block indefinitely.
    #
    # We execute it inside a separate worker with a timeout.
    #
    # If it fails/hangs, the collector keeps using the verified mapping
    # stored in KNOWN_BINARY_ACTIVE_IDS.
    # -------------------------------------------------------------------------

    def _mapping_update_worker(self) -> Any:

        if self.api is None:

            raise RuntimeError(
                "IQ Option API object does not exist."
            )

        return self.api.update_ACTIVES_OPCODE()

    def update_binary_mappings(
        self,
        force: bool = False,
    ) -> bool:

        with self.mapping_lock:

            current_time = time.time()

            if (
                not force
                and self.mapping_last_refresh
                and (
                    current_time - self.mapping_last_refresh
                    < MAPPING_REFRESH_INTERVAL_SECONDS
                )
            ):

                return True

            if not self.is_connected():

                logger.warning(
                    "Cannot refresh Binary mappings because "
                    "IQ Option is not connected."
                )

                return False

            logger.info(
                "Updating Binary active mappings "
                "(timeout: %ss)...",
                MAPPING_TIMEOUT_SECONDS,
            )

            executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="firefly-mapping",
            )

            future = executor.submit(
                self._mapping_update_worker
            )

            try:

                future.result(
                    timeout=MAPPING_TIMEOUT_SECONDS
                )

                executor.shutdown(
                    wait=False,
                    cancel_futures=False,
                )

                refreshed_mapping = {}

                for pair in PAIRS:

                    try:

                        active_id = self.api.get_active_id(pair)

                    except Exception as error:

                        logger.warning(
                            "Could not obtain active ID for %s: %s",
                            pair,
                            error,
                        )

                        active_id = None

                    if active_id:

                        refreshed_mapping[pair] = int(active_id)

                # -------------------------------------------------------------
                # Only replace our mapping if we received useful data.
                # -------------------------------------------------------------

                if refreshed_mapping:

                    for pair, active_id in refreshed_mapping.items():

                        self.mapping[pair] = active_id

                self.mapping_last_refresh = current_time

                logger.info(
                    "Binary mapping update completed."
                )

                for pair in PAIRS:

                    active_id = self.mapping.get(pair)

                    if active_id:

                        logger.info(
                            "  %s -> %s",
                            pair,
                            active_id,
                        )

                    else:

                        logger.warning(
                            "  %s -> MISSING",
                            pair,
                        )

                return True

            except FuturesTimeoutError:

                logger.error(
                    "Binary mapping update timed out after %s seconds.",
                    MAPPING_TIMEOUT_SECONDS,
                )

                logger.warning(
                    "Keeping previously verified Binary mappings "
                    "and continuing candle collection."
                )

                executor.shutdown(
                    wait=False,
                    cancel_futures=False,
                )

                self.mapping_last_refresh = current_time

                return False

            except Exception as error:

                logger.error(
                    "Binary mapping update failed: %s",
                    error,
                )

                logger.warning(
                    "Keeping previously verified Binary mappings."
                )

                executor.shutdown(
                    wait=False,
                    cancel_futures=False,
                )

                self.mapping_last_refresh = current_time

                return False

    # -------------------------------------------------------------------------
    # GET ACTIVE ID
    # -------------------------------------------------------------------------

    def get_active_id(
        self,
        asset: str,
    ) -> Optional[int]:

        return self.mapping.get(asset)

    # -------------------------------------------------------------------------
    # GET CANDLES
    # -------------------------------------------------------------------------

    def get_candles(
        self,
        asset: str,
        count: int,
        end_time: Optional[int] = None,
    ) -> Optional[list[dict[str, Any]]]:

        if self.api is None:

            logger.error(
                "Cannot request candles: API object is not available."
            )

            return None

        if end_time is None:

            end_time = int(time.time())

        try:

            candles = self.api.get_candles(
                asset,
                TIMEFRAME_SECONDS,
                count,
                end_time,
            )

            if candles is None:

                return None

            return candles

        except Exception as error:

            logger.warning(
                "Candle request failed for %s: %s",
                asset,
                error,
            )

            return None


# =============================================================================
# CANDLE COLLECTOR
# =============================================================================

class CandleCollector:

    def __init__(
        self,
        database: CandleDatabase,
        client: IQOptionClient,
    ):

        self.database = database
        self.client = client

        self.stop_event = Event()

        self.last_candle_timestamp: dict[str, int] = {}

        self.last_mapping_refresh_attempt = 0

        self.connection_failure_count = 0

    # -------------------------------------------------------------------------
    # SHUTDOWN REQUEST
    # -------------------------------------------------------------------------

    def request_shutdown(
        self,
        signum: Optional[int] = None,
        frame: Any = None,
    ) -> None:

        logger.info(
            "Shutdown requested."
        )

        self.stop_event.set()

    # -------------------------------------------------------------------------
    # PARSE CANDLE
    # -------------------------------------------------------------------------

    def parse_candle(
        self,
        pair: str,
        raw: dict[str, Any],
    ) -> Optional[Candle]:

        try:

            timestamp = (
                safe_int(raw.get("from"))
                or safe_int(raw.get("at"))
                or safe_int(raw.get("timestamp"))
            )

            if timestamp is None:
                return None

            open_price = safe_float(
                raw.get("open")
            )

            close_price = safe_float(
                raw.get("close")
            )

            high_price = safe_float(
                raw.get("max")
            )

            if high_price is None:
                high_price = safe_float(
                    raw.get("high")
                )

            low_price = safe_float(
                raw.get("min")
            )

            if low_price is None:
                low_price = safe_float(
                    raw.get("low")
                )

            volume = safe_float(
                raw.get("volume")
            )

            if (
                open_price is None
                or close_price is None
                or high_price is None
                or low_price is None
            ):

                return None

            if high_price < max(open_price, close_price):

                high_price = max(
                    high_price,
                    open_price,
                    close_price,
                )

            if low_price > min(open_price, close_price):

                low_price = min(
                    low_price,
                    open_price,
                    close_price,
                )

            if high_price < low_price:

                return None

            color = candle_color(
                open_price,
                close_price,
            )

            return Candle(
                pair=pair,
                asset=pair,
                timestamp=timestamp,
                timestamp_wat=timestamp_to_wat(timestamp),
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                volume=volume,
                candle_color=color,
            )

        except Exception as error:

            logger.warning(
                "Could not parse candle for %s: %s",
                pair,
                error,
            )

            return None

    # -------------------------------------------------------------------------
    # FILTER COMPLETED CANDLES
    # -------------------------------------------------------------------------

    def filter_completed_candles(
        self,
        candles: list[Candle],
    ) -> list[Candle]:

        if not candles:
            return []

        # The current minute is still forming.
        current_minute = (
            int(time.time()) // TIMEFRAME_SECONDS
        ) * TIMEFRAME_SECONDS

        completed = []

        for candle in candles:

            if candle.timestamp >= current_minute:
                continue

            completed.append(candle)

        completed.sort(
            key=lambda candle: candle.timestamp
        )

        return completed

    # -------------------------------------------------------------------------
    # INITIAL HISTORY
    # -------------------------------------------------------------------------

    def collect_initial_history(
        self,
        pair: str,
    ) -> bool:

        logger.info(
            "[%s] Requesting initial %s-candle history...",
            pair,
            INITIAL_CANDLES,
        )

        for attempt in range(1, MAX_CANDLE_RETRIES + 1):

            if self.stop_event.is_set():
                return False

            candles_raw = self.client.get_candles(
                pair,
                INITIAL_CANDLES,
            )

            if candles_raw is not None:

                parsed = []

                for raw in candles_raw:

                    candle = self.parse_candle(
                        pair,
                        raw,
                    )

                    if candle:
                        parsed.append(candle)

                completed = self.filter_completed_candles(
                    parsed
                )

                inserted = self.database.insert_candles(
                    completed
                )

                if completed:

                    latest_timestamp = completed[-1].timestamp

                    self.last_candle_timestamp[pair] = (
                        latest_timestamp
                    )

                    self.database.update_state(
                        pair,
                        latest_timestamp,
                        inserted,
                    )

                    logger.info(
                        "[%s] Initial history: %s candles received, "
                        "%s inserted. Latest: %s WAT",
                        pair,
                        len(completed),
                        inserted,
                        completed[-1].timestamp_wat,
                    )

                else:

                    logger.warning(
                        "[%s] Initial history request returned "
                        "no completed candles.",
                        pair,
                    )

                return True

            logger.warning(
                "[%s] Initial candle request failed "
                "(attempt %s/%s).",
                pair,
                attempt,
                MAX_CANDLE_RETRIES,
            )

            if attempt < MAX_CANDLE_RETRIES:

                self.stop_event.wait(
                    CANDLE_RETRY_DELAY_SECONDS
                )

        self.database.update_state(
            pair,
            self.last_candle_timestamp.get(pair),
            error="Initial candle request failed.",
        )

        return False

    # -------------------------------------------------------------------------
    # COLLECT LATEST CANDLES
    # -------------------------------------------------------------------------

    def collect_latest_candles(
        self,
        pair: str,
    ) -> bool:

        for attempt in range(1, MAX_CANDLE_RETRIES + 1):

            if self.stop_event.is_set():
                return False

            candles_raw = self.client.get_candles(
                pair,
                LATEST_CANDLES,
            )

            if candles_raw is not None:

                parsed = []

                for raw in candles_raw:

                    candle = self.parse_candle(
                        pair,
                        raw,
                    )

                    if candle:
                        parsed.append(candle)

                completed = self.filter_completed_candles(
                    parsed
                )

                if not completed:

                    return True

                latest_timestamp = (
                    self.last_candle_timestamp.get(pair)
                )

                new_candles = []

                for candle in completed:

                    if (
                        latest_timestamp is None
                        or candle.timestamp > latest_timestamp
                    ):

                        new_candles.append(candle)

                if new_candles:

                    inserted = self.database.insert_candles(
                        new_candles
                    )

                    newest = new_candles[-1]

                    self.last_candle_timestamp[pair] = (
                        newest.timestamp
                    )

                    self.database.update_state(
                        pair,
                        newest.timestamp,
                        inserted,
                    )

                    logger.info(
                        "[%s] New candle: %s | "
                        "O=%.6f H=%.6f L=%.6f C=%.6f | %s | "
                        "inserted=%s",
                        pair,
                        newest.timestamp_wat,
                        newest.open,
                        newest.high,
                        newest.low,
                        newest.close,
                        newest.candle_color,
                        inserted,
                    )

                return True

            logger.warning(
                "[%s] Latest candle request failed "
                "(attempt %s/%s).",
                pair,
                attempt,
                MAX_CANDLE_RETRIES,
            )

            if attempt < MAX_CANDLE_RETRIES:

                self.stop_event.wait(
                    CANDLE_RETRY_DELAY_SECONDS
                )

        self.database.update_state(
            pair,
            self.last_candle_timestamp.get(pair),
            error="Latest candle request failed.",
        )

        return False

    # -------------------------------------------------------------------------
    # INITIALIZE PAIRS
    # -------------------------------------------------------------------------

    def initialize_pairs(self) -> None:

        logger.info(
            "Initializing %s Binary Forex pairs...",
            len(PAIRS),
        )

        for pair in PAIRS:

            if self.stop_event.is_set():
                return

            active_id = self.client.get_active_id(
                pair
            )

            if active_id:

                logger.info(
                    "[%s] Binary active ID: %s",
                    pair,
                    active_id,
                )

            else:

                logger.error(
                    "[%s] No Binary active ID available.",
                    pair,
                )

                continue

            self.collect_initial_history(
                pair
            )

    # -------------------------------------------------------------------------
    # ENSURE CONNECTION
    # -------------------------------------------------------------------------

    def ensure_connection(self) -> bool:

        if self.client.is_connected():

            self.connection_failure_count = 0

            return True

        self.connection_failure_count += 1

        logger.warning(
            "IQ Option connection is unavailable."
        )

        logger.info(
            "Attempting automatic reconnection "
            "(failure count: %s)...",
            self.connection_failure_count,
        )

        connected = self.client.connect()

        if connected:

            self.connection_failure_count = 0

            logger.info(
                "IQ Option reconnection successful."
            )

            self.database.log_event(
                "RECONNECTED",
                None,
                "IQ Option connection restored.",
            )

            # Refresh mappings after reconnect.
            self.client.update_binary_mappings(
                force=True
            )

            return True

        delay = min(
            MAX_RECONNECT_DELAY_SECONDS,
            RECONNECT_DELAY_SECONDS
            * min(
                self.connection_failure_count,
                6,
            ),
        )

        logger.warning(
            "Reconnection failed. "
            "Retrying in %s seconds...",
            delay,
        )

        self.database.log_event(
            "CONNECTION_FAILURE",
            None,
            f"IQ Option reconnection failed. "
            f"Retrying in {delay} seconds.",
        )

        self.stop_event.wait(delay)

        return False

    # -------------------------------------------------------------------------
    # PERIODIC MAPPING REFRESH
    # -------------------------------------------------------------------------

    def refresh_mappings_if_needed(self) -> None:

        current_time = time.time()

        if (
            current_time
            - self.last_mapping_refresh_attempt
            < MAPPING_REFRESH_INTERVAL_SECONDS
        ):

            return

        self.last_mapping_refresh_attempt = current_time

        logger.info(
            "Performing scheduled Binary mapping refresh..."
        )

        success = self.client.update_binary_mappings(
            force=True
        )

        if success:

            logger.info(
                "Scheduled Binary mapping refresh succeeded."
            )

        else:

            logger.warning(
                "Scheduled Binary mapping refresh failed "
                "or timed out. Existing mappings remain active."
            )

    # -------------------------------------------------------------------------
    # MAIN LOOP
    # -------------------------------------------------------------------------

    def run(self) -> None:

        logger.info("=" * 80)
        logger.info(
            "FIREFLY AI - BINARY CANDLE COLLECTOR"
        )
        logger.info("=" * 80)

        logger.info(
            "Market: REAL Binary Forex"
        )

        logger.info(
            "Timeframe: %s seconds",
            TIMEFRAME_SECONDS,
        )

        logger.info(
            "Timezone: %s",
            TIMEZONE_NAME,
        )

        logger.info(
            "Pairs: %s",
            ", ".join(PAIRS),
        )

        logger.info(
            "Database: %s",
            DATABASE_FILE,
        )

        logger.info(
            "Polling interval: %s seconds",
            POLL_INTERVAL_SECONDS,
        )

        logger.info(
            "OTC fallback: DISABLED"
        )

        logger.info("=" * 80)

        # ---------------------------------------------------------------------
        # CONNECTION LOOP
        # ---------------------------------------------------------------------

        connection_attempt = 0

        while not self.stop_event.is_set():

            connection_attempt += 1

            logger.info(
                "Connecting to IQ Option "
                "(attempt %s)...",
                connection_attempt,
            )

            if self.client.connect():

                break

            delay = min(
                MAX_CONNECT_RETRY_DELAY_SECONDS,
                INITIAL_CONNECT_RETRY_DELAY_SECONDS
                * min(
                    connection_attempt,
                    6,
                ),
            )

            logger.warning(
                "Initial IQ Option connection failed."
            )

            logger.warning(
                "Firefly will retry in %s seconds.",
                delay,
            )

            self.database.log_event(
                "CONNECTION_FAILURE",
                None,
                (
                    "Initial IQ Option connection failed. "
                    f"Retrying in {delay} seconds."
                ),
            )

            self.stop_event.wait(delay)

        if self.stop_event.is_set():

            logger.info(
                "Shutdown requested before connection completed."
            )

            return

        # ---------------------------------------------------------------------
        # BINARY MAPPING
        # ---------------------------------------------------------------------

        logger.info(
            "Attempting Binary active mapping initialization..."
        )

        mapping_success = self.client.update_binary_mappings(
            force=True
        )

        if mapping_success:

            logger.info(
                "Fresh Binary mappings loaded successfully."
            )

        else:

            logger.warning(
                "Fresh Binary mapping initialization failed "
                "or timed out."
            )

            logger.warning(
                "Using previously verified Binary active IDs."
            )

        # ---------------------------------------------------------------------
        # DISPLAY ACTIVE MAPPINGS
        # ---------------------------------------------------------------------

        logger.info("=" * 80)
        logger.info(
            "ACTIVE REAL BINARY FOREX MAPPINGS"
        )
        logger.info("=" * 80)

        for pair in PAIRS:

            active_id = self.client.get_active_id(
                pair
            )

            if active_id:

                logger.info(
                    "%-10s -> %s",
                    pair,
                    active_id,
                )

            else:

                logger.error(
                    "%-10s -> MISSING",
                    pair,
                )

        logger.info("=" * 80)

        # ---------------------------------------------------------------------
        # INITIAL HISTORY
        # ---------------------------------------------------------------------

        self.initialize_pairs()

        # ---------------------------------------------------------------------
        # NORMAL COLLECTION LOOP
        # ---------------------------------------------------------------------

        logger.info("=" * 80)
        logger.info(
            "LIVE CANDLE COLLECTION STARTED"
        )
        logger.info("=" * 80)

        self.database.log_event(
            "COLLECTOR_STARTED",
            None,
            "Live Binary candle collection started.",
        )

        while not self.stop_event.is_set():

            # ---------------------------------------------------------------
            # CONNECTION
            # ---------------------------------------------------------------

            if not self.ensure_connection():

                continue

            # ---------------------------------------------------------------
            # PERIODIC MAPPING REFRESH
            # ---------------------------------------------------------------

            self.refresh_mappings_if_needed()

            # ---------------------------------------------------------------
            # COLLECT CANDLES
            # ---------------------------------------------------------------

            any_failure = False

            for pair in PAIRS:

                if self.stop_event.is_set():
                    break

                active_id = self.client.get_active_id(
                    pair
                )

                if not active_id:

                    logger.error(
                        "[%s] No Binary active ID available. "
                        "Skipping this cycle.",
                        pair,
                    )

                    any_failure = True

                    continue

                success = self.collect_latest_candles(
                    pair
                )

                if not success:

                    any_failure = True

            # ---------------------------------------------------------------
            # WAIT
            # ---------------------------------------------------------------

            if not self.stop_event.is_set():

                self.stop_event.wait(
                    POLL_INTERVAL_SECONDS
                )

        logger.info(
            "Live candle collection loop stopped."
        )

    # -------------------------------------------------------------------------
    # SHUTDOWN
    # -------------------------------------------------------------------------

    def shutdown(self) -> None:

        logger.info(
            "Shutting down Firefly candle collector..."
        )

        try:

            self.client.disconnect()

        except Exception as error:

            logger.warning(
                "Error shutting down IQ Option client: %s",
                error,
            )

        self.database.log_event(
            "COLLECTOR_STOPPED",
            None,
            "Firefly candle collector stopped.",
        )

        self.database.close()

        logger.info(
            "Firefly candle collector stopped."
        )


# =============================================================================
# ENVIRONMENT
# =============================================================================

def load_credentials() -> tuple[str, str]:

    load_dotenv(
        dotenv_path=ENV_FILE
    )

    email = os.getenv(
        "IQ_EMAIL"
    )

    password = os.getenv(
        "IQ_PASSWORD"
    )

    if not email:

        raise RuntimeError(
            f"IQ_EMAIL is missing from {ENV_FILE}"
        )

    if not password:

        raise RuntimeError(
            f"IQ_PASSWORD is missing from {ENV_FILE}"
        )

    logger.info(
        "IQ Option credentials loaded."
    )

    return email, password


# =============================================================================
# SIGNAL HANDLERS
# =============================================================================

def install_signal_handlers(
    collector: CandleCollector,
) -> None:

    signal.signal(
        signal.SIGINT,
        collector.request_shutdown,
    )

    if hasattr(signal, "SIGTERM"):

        signal.signal(
            signal.SIGTERM,
            collector.request_shutdown,
        )


# =============================================================================
# MAIN
# =============================================================================

def main() -> None:

    logger.info("=" * 80)
    logger.info(
        "FIREFLY AI - IQ OPTION BINARY CANDLE COLLECTOR"
    )
    logger.info("=" * 80)

    logger.info(
        "Python: %s",
        sys.version.split()[0],
    )

    logger.info(
        "Database: %s",
        DATABASE_FILE,
    )

    logger.info(
        "Market: REAL Binary Forex"
    )

    logger.info(
        "Pairs: %s",
        ", ".join(PAIRS),
    )

    logger.info(
        "Timeframe: 1 minute"
    )

    logger.info(
        "Timezone: %s",
        TIMEZONE_NAME,
    )

    logger.info(
        "OTC fallback: DISABLED"
    )

    logger.info("=" * 80)

    database: Optional[CandleDatabase] = None
    client: Optional[IQOptionClient] = None
    collector: Optional[CandleCollector] = None

    try:

        # ---------------------------------------------------------------------
        # LOAD CREDENTIALS
        # ---------------------------------------------------------------------

        email, password = load_credentials()

        # ---------------------------------------------------------------------
        # DATABASE
        # ---------------------------------------------------------------------

        database = CandleDatabase(
            DATABASE_FILE
        )

        # ---------------------------------------------------------------------
        # IQ OPTION CLIENT
        # ---------------------------------------------------------------------

        client = IQOptionClient(
            email,
            password,
        )

        # ---------------------------------------------------------------------
        # COLLECTOR
        # ---------------------------------------------------------------------

        collector = CandleCollector(
            database,
            client,
        )

        install_signal_handlers(
            collector
        )

        # ---------------------------------------------------------------------
        # RUN
        # ---------------------------------------------------------------------

        collector.run()

    except KeyboardInterrupt:

        logger.info(
            "Keyboard interrupt received."
        )

    except Exception as error:

        logger.exception(
            "Fatal collector error: %s",
            error,
        )

    finally:

        if collector is not None:

            collector.shutdown()

        elif client is not None:

            client.disconnect()

        elif database is not None:

            database.close()


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    main()

