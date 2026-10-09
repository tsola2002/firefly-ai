"""
Firefly AI - Pocket Option OTC Market Scanner

Data source:
    Pocket Option unofficial WebSocket client

Strategy:
    - 1-minute candles
    - 15 completed candles requested
    - 7 consecutive GREEN candles -> REVERSAL DOWN
    - 7 consecutive RED candles -> REVERSAL UP
    - DOJI breaks the streak
    - Signal only - NO TRADE EXECUTION

OTC pairs:
    USDJPY_otc
    AUDUSD_otc
    EURJPY_otc
    AUDJPY_otc
    GBPJPY_otc
    GBPAUD_otc
    GBPCAD_otc
    CADJPY_otc

Telegram:
    HTML formatted notifications
    Country flags for both currencies

Timezone:
    Africa/Lagos / WAT (UTC+1)

IMPORTANT:
    This uses an unofficial Pocket Option WebSocket client.
    It is not an official Pocket Option API.
"""

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from pocketoptionapi import PocketOption


# ============================================================
# ENVIRONMENT
# ============================================================

load_dotenv()


PO_SSID = os.getenv("PO_SSID", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()


# ============================================================
# STRATEGY SETTINGS
# ============================================================

TIMEFRAME = 60                  # 1-minute candles
CANDLE_COUNT = 15               # Candles requested
MIN_STREAK = 7                   # Signal after 7 same-color candles
POLL_INTERVAL = 15               # Scan every 15 seconds

# Large offset recommended by the current PocketOptionApi
HISTORY_OFFSET = 9000

# Number of historical requests
HISTORY_REQUESTS = 1


# ============================================================
# POCKET OPTION OTC PAIRS
# ============================================================

OTC_PAIRS = [
    "USDJPY_otc",
    "AUDUSD_otc",
    "EURJPY_otc",
    "AUDJPY_otc",
    "GBPJPY_otc",
    "GBPAUD_otc",
    "GBPCAD_otc",
    "CADJPY_otc",
]


# ============================================================
# COUNTRY FLAGS
# ============================================================

CURRENCY_FLAGS = {
    "USD": "🇺🇸",
    "JPY": "🇯🇵",
    "AUD": "🇦🇺",
    "EUR": "🇪🇺",
    "GBP": "🇬🇧",
    "CAD": "🇨🇦",
    "CHF": "🇨🇭",
    "NZD": "🇳🇿",
}


# ============================================================
# TIMEZONE
# ============================================================

WAT = timezone(timedelta(hours=1), name="WAT")


# ============================================================
# GLOBAL STATE
# ============================================================

api: Optional[PocketOption] = None

running = True

# Prevent the same candle from generating the same signal repeatedly.
LAST_SIGNAL_CANDLE: Dict[str, int] = {}

# Track last candle timestamp processed for logging/debugging.
LAST_CANDLE_TIMESTAMP: Dict[str, int] = {}

# Resolved live asset symbols.
RESOLVED_ASSETS: Dict[str, str] = {}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("firefly-pocket-option")


# ============================================================
# VALIDATION
# ============================================================

def validate_environment() -> None:
    """
    Validate required environment variables.
    """

    missing = []

    if not PO_SSID:
        missing.append("PO_SSID")

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        raise RuntimeError(
            "Missing required environment variables: "
            + ", ".join(missing)
        )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message: str) -> bool:
    """
    Send an HTML formatted Telegram message.
    """

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            url,
            data=payload,
            timeout=15,
        )

        if response.ok:
            logger.info("Telegram notification sent.")
            return True

        logger.error(
            "Telegram error: HTTP %s - %s",
            response.status_code,
            response.text,
        )

        return False

    except requests.RequestException as exc:
        logger.error("Telegram request failed: %s", exc)
        return False


# ============================================================
# PAIR HELPERS
# ============================================================

def normalize_pair(pair: str) -> str:
    """
    Convert Pocket Option OTC symbol to display form.

    EURUSD_otc -> EUR/USD OTC
    """

    clean = pair.upper().replace("_OTC", "").replace("-OTC", "")

    if len(clean) >= 6:
        return f"{clean[:3]}/{clean[3:6]} OTC"

    return pair.upper()


def get_currencies(pair: str) -> Tuple[str, str]:
    """
    Extract base and quote currencies.
    """

    clean = (
        pair.upper()
        .replace("_OTC", "")
        .replace("-OTC", "")
    )

    if len(clean) >= 6:
        return clean[:3], clean[3:6]

    return "", ""


def get_pair_flags(pair: str) -> str:
    """
    Return the two respective country/currency flags.
    """

    base, quote = get_currencies(pair)

    base_flag = CURRENCY_FLAGS.get(base, "🏳️")
    quote_flag = CURRENCY_FLAGS.get(quote, "🏳️")

    return f"{base_flag} {quote_flag}"


# ============================================================
# CANDLE HELPERS
# ============================================================

def candle_color(candle: dict) -> str:
    """
    Determine candle color.

    GREEN:
        close > open

    RED:
        close < open

    DOJI:
        close == open
    """

    try:
        candle_open = float(candle["open"])
        candle_close = float(candle["close"])
    except (KeyError, TypeError, ValueError):
        return "DOJI"

    if candle_close > candle_open:
        return "GREEN"

    if candle_close < candle_open:
        return "RED"

    return "DOJI"


def get_candle_timestamp(candle: dict) -> int:
    """
    Get normalized candle timestamp.
    """

    value = candle.get("time")

    if value is None:
        value = candle.get("timestamp")

    if value is None:
        return 0

    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def get_completed_candles(
    candles: List[dict],
) -> List[dict]:
    """
    Return only completed 1-minute candles.

    The current PocketOptionApi already reconciles historical
    candles against realtime data and removes the forming candle.

    This second defensive check makes sure we never accidentally
    use a candle whose close time is still in the future.
    """

    if not candles:
        return []

    # Sort chronologically.
    sorted_candles = sorted(
        candles,
        key=get_candle_timestamp,
    )

    now = int(time.time())

    completed = []

    for candle in sorted_candles:

        timestamp = get_candle_timestamp(candle)

        if timestamp <= 0:
            continue

        # A 60-second candle beginning at timestamp is completed
        # only after timestamp + TIMEFRAME.
        if timestamp + TIMEFRAME <= now:
            completed.append(candle)

    return completed


# ============================================================
# STREAK ANALYSIS
# ============================================================

def analyze_streak(
    candles: List[dict],
) -> Tuple[Optional[str], int, Optional[dict]]:
    """
    Analyze the most recent candle streak.

    Returns:

        signal_direction:
            REVERSAL DOWN
            REVERSAL UP
            None

        streak_count:
            Current consecutive streak length

        signal_candle:
            Candle responsible for the signal
    """

    if len(candles) < MIN_STREAK:
        return None, 0, None

    recent = candles[-MIN_STREAK:]

    colors = [
        candle_color(candle)
        for candle in recent
    ]

    # Any DOJI invalidates the streak.
    if "DOJI" in colors:
        return None, 0, None

    # Seven GREEN candles.
    if all(color == "GREEN" for color in colors):
        return (
            "REVERSAL DOWN",
            MIN_STREAK,
            recent[-1],
        )

    # Seven RED candles.
    if all(color == "RED" for color in colors):
        return (
            "REVERSAL UP",
            MIN_STREAK,
            recent[-1],
        )

    # Calculate current streak for logging.
    latest_color = colors[-1]
    streak = 0

    for color in reversed(colors):
        if color == latest_color:
            streak += 1
        else:
            break

    return None, streak, None


# ============================================================
# TELEGRAM SIGNAL MESSAGE
# ============================================================

def build_signal_message(
    asset: str,
    signal: str,
    streak: int,
    candle: dict,
) -> str:
    """
    Build Telegram signal message.
    """

    pair_display = normalize_pair(asset)
    flags = get_pair_flags(asset)

    timestamp = get_candle_timestamp(candle)

    if timestamp:
        candle_time = datetime.fromtimestamp(
            timestamp,
            tz=WAT,
        ).strftime("%Y-%m-%d %H:%M:%S")
    else:
        candle_time = datetime.now(WAT).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    candle_open = candle.get("open", 0)
    candle_close = candle.get("close", 0)

    if signal == "REVERSAL DOWN":
        action = "SELL / LOWER"
        emoji = "🔴"
    else:
        action = "BUY / HIGHER"
        emoji = "🟢"

    return (
        f"🚨 <b>FIREFLY AI OTC SIGNAL</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{flags} <b>{pair_display}</b>\n\n"
        f"📊 <b>Strategy:</b> 7-Candle Reversal\n"
        f"🕯️ <b>Timeframe:</b> 1 Minute\n"
        f"🔥 <b>Streak:</b> {streak} consecutive candles\n"
        f"📈 <b>Signal:</b> {signal}\n"
        f"{emoji} <b>Action:</b> {action}\n"
        f"⏱️ <b>Candle:</b> {candle_time} WAT\n\n"
        f"Open: <code>{candle_open}</code>\n"
        f"Close: <code>{candle_close}</code>\n\n"
        f"⏳ <b>Expiry reference:</b> 1 minute\n"
        f"⚠️ <i>SIGNAL ONLY — NO TRADE EXECUTED</i>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )


# ============================================================
# POCKET OPTION CONNECTION
# ============================================================

def connect_pocket_option() -> PocketOption:
    """
    Create and connect a Pocket Option WebSocket client.
    """

    global api

    logger.info("=" * 70)
    logger.info("CONNECTING TO POCKET OPTION")
    logger.info("=" * 70)

    client = PocketOption(PO_SSID)

    logger.info("Opening Pocket Option WebSocket...")

    success, error = client.connect()

    if not success:
        raise ConnectionError(
            f"Pocket Option connection failed: {error}"
        )

    logger.info("WebSocket connected.")

    # Historical candle requests require server time synchronization.
    deadline = time.time() + 30

    while time.time() < deadline:

        if client.check_connect() and client.is_time_synced():
            break

        time.sleep(0.25)

    if not client.check_connect():
        raise ConnectionError(
            "Pocket Option WebSocket disconnected during startup."
        )

    if not client.is_time_synced():
        raise ConnectionError(
            "Pocket Option server time could not be synchronized."
        )

    api = client

    server_time = datetime.fromtimestamp(
        client.get_server_timestamp(),
        tz=timezone.utc,
    )

    logger.info(
        "Pocket Option server time: %s UTC",
        server_time.strftime("%Y-%m-%d %H:%M:%S"),
    )

    logger.info("Pocket Option connection ready.")

    return client


# ============================================================
# ASSET DISCOVERY
# ============================================================

def resolve_assets(client: PocketOption) -> Dict[str, str]:
    """
    Resolve requested OTC assets against Pocket Option's
    live asset catalog.

    We deliberately use the live catalog rather than hardcoding
    broker asset IDs.
    """

    logger.info("=" * 70)
    logger.info("DISCOVERING POCKET OPTION OTC ASSETS")
    logger.info("=" * 70)

    assets = client.get_assets()

    if not assets:
        raise RuntimeError(
            "Pocket Option returned an empty asset catalog."
        )

    logger.info(
        "Pocket Option returned %d assets.",
        len(assets),
    )

    resolved = {}

    available_symbols = {
        symbol.upper(): symbol
        for symbol in assets.keys()
    }

    for requested in OTC_PAIRS:

        actual = available_symbols.get(
            requested.upper()
        )

        if actual:
            info = assets.get(actual, {})

            is_available = info.get(
                "is_available",
                True,
            )

            payout = info.get(
                "payout"
            )

            if is_available:
                resolved[requested] = actual

                logger.info(
                    "✓ %-12s -> %-12s | payout=%s",
                    requested,
                    actual,
                    payout,
                )
            else:
                logger.warning(
                    "✗ %-12s exists but is unavailable.",
                    requested,
                )

        else:
            logger.warning(
                "✗ %-12s not found in live asset catalog.",
                requested,
            )

    return resolved


# ============================================================
# SUBSCRIPTIONS
# ============================================================

def subscribe_assets(
    client: PocketOption,
    assets: Dict[str, str],
) -> None:
    """
    Subscribe to realtime stream data for each OTC asset.

    Historical candle reconciliation uses these realtime
    ticks to improve the most recent bars.
    """

    logger.info("=" * 70)
    logger.info("SUBSCRIBING TO OTC STREAMS")
    logger.info("=" * 70)

    for requested, actual in assets.items():

        try:

            success = client.subscribe(
                actual,
                period=TIMEFRAME,
            )

            if success:
                logger.info(
                    "✓ Subscribed: %s",
                    actual,
                )
            else:
                logger.warning(
                    "✗ Subscription failed: %s",
                    actual,
                )

            # Give the server a moment between subscriptions.
            time.sleep(0.25)

        except Exception as exc:

            logger.error(
                "Subscription error for %s: %s",
                actual,
                exc,
            )


# ============================================================
# HISTORICAL CANDLES
# ============================================================

def fetch_candles(
    client: PocketOption,
    asset: str,
) -> List[dict]:
    """
    Retrieve recent Pocket Option OTC candles.

    The current unofficial client returns candle dictionaries
    containing time/open/high/low/close/volume.
    """

    try:

        candles = client.get_historical_candles(
            asset,
            period=TIMEFRAME,
            offset=HISTORY_OFFSET,
            count_request=HISTORY_REQUESTS,
        )

        if not candles:
            return []

        completed = get_completed_candles(candles)

        # We only need the latest CANDLE_COUNT completed candles.
        return completed[-CANDLE_COUNT:]

    except Exception as exc:

        logger.error(
            "Candle request failed for %s: %s",
            asset,
            exc,
        )

        return []


# ============================================================
# CANDLE DISPLAY
# ============================================================

def log_candle_state(
    asset: str,
    candles: List[dict],
) -> None:
    """
    Log current candle streak.
    """

    if not candles:
        return

    colors = [
        candle_color(candle)
        for candle in candles
    ]

    latest_color = colors[-1]

    streak = 0

    for color in reversed(colors):

        if color == latest_color:
            streak += 1

        else:
            break

    latest = candles[-1]

    timestamp = get_candle_timestamp(latest)

    candle_time = datetime.fromtimestamp(
        timestamp,
        tz=WAT,
    ).strftime("%H:%M:%S")

    logger.info(
        "%-12s | latest=%s | streak=%d | candle=%s",
        normalize_pair(asset),
        latest_color,
        streak,
        candle_time,
    )


# ============================================================
# SIGNAL PROCESSING
# ============================================================

def process_asset(
    client: PocketOption,
    requested_asset: str,
    actual_asset: str,
) -> None:
    """
    Fetch candles and process the strategy for one asset.
    """

    candles = fetch_candles(
        client,
        actual_asset,
    )

    if not candles:
        logger.warning(
            "%s -> no completed candles received.",
            requested_asset,
        )
        return

    if len(candles) < MIN_STREAK:
        logger.warning(
            "%s -> only %d completed candles received.",
            requested_asset,
            len(candles),
        )
        return

    log_candle_state(
        requested_asset,
        candles,
    )

    signal_direction, streak, signal_candle = analyze_streak(
        candles
    )

    if signal_direction is None:
        return

    if signal_candle is None:
        return

    signal_timestamp = get_candle_timestamp(
        signal_candle
    )

    if signal_timestamp <= 0:
        return

    # --------------------------------------------------------
    # DUPLICATE PROTECTION
    # --------------------------------------------------------

    previous_signal_timestamp = LAST_SIGNAL_CANDLE.get(
        requested_asset
    )

    if previous_signal_timestamp == signal_timestamp:

        logger.debug(
            "Duplicate signal ignored: %s candle=%s",
            requested_asset,
            signal_timestamp,
        )

        return

    # --------------------------------------------------------
    # SAVE SIGNAL CANDLE
    # --------------------------------------------------------

    LAST_SIGNAL_CANDLE[
        requested_asset
    ] = signal_timestamp

    # --------------------------------------------------------
    # SEND TELEGRAM
    # --------------------------------------------------------

    message = build_signal_message(
        requested_asset,
        signal_direction,
        streak,
        signal_candle,
    )

    send_telegram(message)

    logger.warning(
        "SIGNAL: %s | %s | %s",
        requested_asset,
        signal_direction,
        signal_timestamp,
    )


# ============================================================
# CONNECTION CHECK
# ============================================================

def ensure_connection(
    client: PocketOption,
) -> bool:
    """
    Check whether the WebSocket is still connected and
    synchronized.
    """

    try:

        if not client.check_connect():
            return False

        if not client.is_time_synced():
            return False

        return True

    except Exception as exc:

        logger.error(
            "Connection health check failed: %s",
            exc,
        )

        return False


# ============================================================
# STARTUP MESSAGE
# ============================================================

def send_startup_message(
    assets: Dict[str, str],
) -> None:
    """
    Notify Telegram that the scanner has started.
    """

    pairs = "\n".join(
        f"• {get_pair_flags(asset)} "
        f"{normalize_pair(asset)}"
        for asset in assets.keys()
    )

    message = (
        f"🚀 <b>FIREFLY AI STARTED</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🟣 <b>Broker:</b> Pocket Option\n"
        f"📡 <b>Data:</b> Direct OTC WebSocket\n"
        f"🕯️ <b>Timeframe:</b> 1 Minute\n"
        f"🔥 <b>Strategy:</b> 7-Candle Reversal\n"
        f"📊 <b>Mode:</b> SIGNAL ONLY\n\n"
        f"<b>OTC PAIRS</b>\n"
        f"{pairs}\n\n"
        f"⚠️ <i>No trades will be executed.</i>\n"
        f"━━━━━━━━━━━━━━━━━━"
    )

    send_telegram(message)


# ============================================================
# STOP HANDLER
# ============================================================

def stop_bot(signum=None, frame=None):
    """
    Graceful shutdown.
    """

    global running

    logger.info(
        "Shutdown signal received."
    )

    running = False


# ============================================================
# MAIN SCANNER
# ============================================================

def run_scanner() -> None:
    """
    Main scanner loop.
    """

    global api

    while running:

        client = None

        try:

            # ------------------------------------------------
            # CONNECT
            # ------------------------------------------------

            client = connect_pocket_option()

            # ------------------------------------------------
            # DISCOVER ASSETS
            # ------------------------------------------------

            assets = resolve_assets(
                client
            )

            if not assets:

                raise RuntimeError(
                    "None of the configured OTC pairs "
                    "are currently available."
                )

            RESOLVED_ASSETS.clear()
            RESOLVED_ASSETS.update(assets)

            # ------------------------------------------------
            # SUBSCRIBE
            # ------------------------------------------------

            subscribe_assets(
                client,
                assets,
            )

            # Give the WebSocket time to receive ticks.
            logger.info(
                "Waiting for realtime OTC stream data..."
            )

            time.sleep(2)

            # ------------------------------------------------
            # TELEGRAM STARTUP
            # ------------------------------------------------

            send_startup_message(
                assets
            )

            # ------------------------------------------------
            # SCANNER LOOP
            # ------------------------------------------------

            logger.info("=" * 70)
            logger.info(
                "POCKET OPTION OTC SCANNER STARTED"
            )
            logger.info("=" * 70)

            while running:

                cycle_start = time.time()

                # --------------------------------------------
                # CONNECTION CHECK
                # --------------------------------------------

                if not ensure_connection(client):

                    logger.warning(
                        "Pocket Option WebSocket connection lost."
                    )

                    break

                # --------------------------------------------
                # PROCESS ALL PAIRS
                # --------------------------------------------

                for requested_asset, actual_asset in list(
                    assets.items()
                ):

                    if not running:
                        break

                    try:

                        process_asset(
                            client,
                            requested_asset,
                            actual_asset,
                        )

                    except Exception as exc:

                        logger.exception(
                            "Unhandled error processing %s: %s",
                            requested_asset,
                            exc,
                        )

                # --------------------------------------------
                # POLLING DELAY
                # --------------------------------------------

                elapsed = time.time() - cycle_start

                sleep_time = max(
                    1,
                    POLL_INTERVAL - elapsed,
                )

                logger.info(
                    "Scan cycle completed in %.2fs. "
                    "Next scan in %.2fs.",
                    elapsed,
                    sleep_time,
                )

                time.sleep(sleep_time)

        except KeyboardInterrupt:

            logger.info(
                "Keyboard interrupt received."
            )

            break

        except Exception as exc:

            logger.exception(
                "Scanner error: %s",
                exc,
            )

            if running:

                logger.info(
                    "Reconnecting in 10 seconds..."
                )

                time.sleep(10)

        finally:

            # ----------------------------------------------
            # CLOSE CURRENT WEBSOCKET
            # ----------------------------------------------

            if client is not None:

                try:

                    client.disconnect_websocket()

                    logger.info(
                        "Pocket Option WebSocket closed."
                    )

                except Exception as exc:

                    logger.debug(
                        "WebSocket cleanup error: %s",
                        exc,
                    )

            api = None

    logger.info(
        "Firefly AI Pocket Option scanner stopped."
    )


# ============================================================
# ENTRY POINT
# ============================================================

def main() -> None:
    """
    Application entry point.
    """

    print()
    print("=" * 70)
    print("FIREFLY AI - POCKET OPTION OTC SCANNER")
    print("=" * 70)
    print("Data source : Pocket Option WebSocket")
    print("Timeframe   : 1 minute")
    print("Strategy    : 7-candle reversal")
    print("Pairs       : 8 OTC pairs")
    print("Mode        : SIGNAL ONLY")
    print("=" * 70)
    print()

    try:

        validate_environment()

    except RuntimeError as exc:

        logger.error(str(exc))

        sys.exit(1)

    # ----------------------------------------------
    # GRACEFUL SHUTDOWN
    # ----------------------------------------------

    signal.signal(
        signal.SIGINT,
        stop_bot,
    )

    if hasattr(
        signal,
        "SIGTERM",
    ):
        signal.signal(
            signal.SIGTERM,
            stop_bot,
        )

    # ----------------------------------------------
    # RUN
    # ----------------------------------------------

    run_scanner()


if __name__ == "__main__":
    main()