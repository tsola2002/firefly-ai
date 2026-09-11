import os
import sys
import time
import logging
import threading
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.stable_api as stable_api


# ============================================================
# FIREFLY AI - REAL FOREX BINARY REVERSAL BOT
# ============================================================
#
# IMPORTANT:
#
# This bot operates on REAL FOREX symbols:
#
#     USDJPY
#     AUDUSD
#     EURJPY
#     AUDJPY
#     GBPJPY
#     GBPAUD
#     GBPCAD
#     CADJPY
#
# NOT:
#
#     USDJPY-OTC
#     AUDUSD-OTC
#     etc.
#
# "Binary" is the contract/option type.
# It is NOT part of the underlying symbol.
#
# The bot dynamically discovers the IQ Option active IDs from:
#
#     api_option_init_all
#
# and searches both:
#
#     binary
#     turbo
#
# structures for the requested real Forex symbols.
#
# Strategy:
#
#     7 consecutive GREEN candles -> reversal DOWN
#     7 consecutive RED candles   -> reversal UP
#
# Mode:
#
#     SIGNAL ONLY
#
# No automatic trades are placed.
#
# ============================================================


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ------------------------------------------------------------
# Candle configuration
# ------------------------------------------------------------

TIMEFRAME = 60

CANDLE_COUNT = 15

# Scan every 15 seconds.
#
# This allows the bot to notice a newly completed candle
# without making a request every second.
POLL_INTERVAL = 15


# ------------------------------------------------------------
# Asset initialization
# ------------------------------------------------------------

ASSET_REFRESH_TIMEOUT = 30

RECONNECT_DELAY = 5


# ------------------------------------------------------------
# Strategy
# ------------------------------------------------------------

MIN_STREAK = 7


# ------------------------------------------------------------
# Timezone
# ------------------------------------------------------------

NIGERIA_TIMEZONE = timezone(timedelta(hours=1))


# ============================================================
# REAL FOREX PAIRS
# ============================================================
#
# IMPORTANT:
#
# These are REAL FOREX underlying symbols.
#
# Do NOT append "-OTC".
#
# Example:
#
#     USDJPY
#
# NOT:
#
#     USDJPY-OTC
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

if not TELEGRAM_BOT_TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN is missing from .env")
    sys.exit(1)

if not TELEGRAM_CHAT_ID:
    print("ERROR: TELEGRAM_CHAT_ID is missing from .env")
    sys.exit(1)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("REAL-BINARY-BOT")


# ============================================================
# GLOBAL STATE
# ============================================================

API = None

# Prevent duplicate alerts for the same completed candle.
LAST_SIGNAL_CANDLE = {}

# Prevent simultaneous asset initialization requests.
ASSET_LOCK = threading.Lock()


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    """
    Send a Telegram message.

    Returns:
        True  -> successful
        False -> failed
    """

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=10,
        )

        if response.ok:
            logger.info("Telegram message sent.")
            return True

        logger.error(
            "Telegram error: %s",
            response.text,
        )

    except requests.RequestException as exc:
        logger.error(
            "Telegram request failed: %s",
            exc,
        )

    return False


# ============================================================
# TIME FORMATTING
# ============================================================

def format_time(timestamp):
    """
    Convert Unix timestamp to local Nigeria time.

    Uses explicit timezone handling so the output is
    predictable on Windows regardless of system locale.
    """

    try:
        dt = datetime.fromtimestamp(
            float(timestamp),
            tz=timezone.utc,
        ).astimezone(NIGERIA_TIMEZONE)

        return dt.strftime("%I:%M:%S %p").lstrip("0")

    except Exception:
        return "-"


# ============================================================
# SERVER TIME
# ============================================================

def get_server_time():
    """
    Obtain IQ Option server timestamp.

    Falls back to local Unix time if IQ Option's
    timesync information is unavailable.
    """

    global API

    try:
        if API is not None:

            # First attempt.
            server_timestamp = (
                API.api.timesync.server_timestamp
            )

            if server_timestamp:
                return float(server_timestamp)

    except Exception:
        pass

    try:
        if API is not None:

            # Second possible API implementation.
            server_timestamp = API.get_server_timestamp()

            if server_timestamp:
                return float(server_timestamp)

    except Exception:
        pass

    return time.time()


# ============================================================
# CONNECTION
# ============================================================

def connect_iq_option():
    """
    Establish the IQ Option connection.

    Keeps retrying until a connection is established.

    Returns:
        True once connected.
    """

    global API

    while True:

        try:
            logger.info("")
            logger.info("=" * 75)
            logger.info("CONNECTING TO IQ OPTION")
            logger.info("=" * 75)

            API = IQ_Option(
                IQ_EMAIL,
                IQ_PASSWORD,
            )

            check, reason = API.connect()

            if check:

                logger.info(
                    "IQ Option connection successful."
                )

                # ------------------------------------------------
                # Practice account
                # ------------------------------------------------

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

            logger.error(
                "IQ Option connection failed: %s",
                reason,
            )

        except Exception as exc:

            logger.exception(
                "Connection exception: %s",
                exc,
            )

        logger.info(
            "Retrying connection in %d seconds...",
            RECONNECT_DELAY,
        )

        time.sleep(RECONNECT_DELAY)


# ============================================================
# CONNECTION CHECK
# ============================================================

def connection_alive():
    """
    Check whether the IQ Option websocket connection
    appears to be alive.
    """

    global API

    if API is None:
        return False

    try:
        return bool(API.check_connect())

    except Exception:
        return False


# ============================================================
# NORMALIZE IQ OPTION ACTIVE NAME
# ============================================================

def normalize_asset_name(raw_name):
    """
    Normalize an IQ Option active name.

    Examples:

        binary.USDJPY -> USDJPY
        turbo.USDJPY  -> USDJPY
        USDJPY        -> USDJPY

    Returns:
        normalized symbol
        or None
    """

    if not raw_name:
        return None

    try:
        raw_name = str(raw_name).strip()

        if "." in raw_name:
            raw_name = raw_name.split(
                ".",
                1,
            )[1]

        raw_name = raw_name.strip()

        if raw_name in FOREX_PAIRS:
            return raw_name

    except Exception:
        pass

    return None


# ============================================================
# DYNAMIC REAL FOREX ASSET MAPPING
# ============================================================

def refresh_forex_asset_mappings(force=False):
    """
    Dynamically obtain the current IQ Option active IDs
    for the real Forex symbols.

    This follows the same mapping mechanism used by
    report-binary.py.

    IMPORTANT:

    We deliberately use:

        api_option_init_all

    rather than relying exclusively on:

        update_ACTIVES_OPCODE()

    or:

        get_all_open_time()

    This keeps the mapping logic consistent with the
    working report-binary.py implementation.

    Searches:

        binary
        turbo

    and maps:

        USDJPY
        AUDUSD
        etc.

    into:

        stable_api.OP_code.ACTIVES
    """

    global API

    if API is None:
        logger.error(
            "Cannot refresh asset mappings: API is None."
        )
        return False

    with ASSET_LOCK:

        logger.info("")
        logger.info("=" * 75)
        logger.info("REFRESHING REAL FOREX ASSET MAPPINGS")
        logger.info("=" * 75)

        # --------------------------------------------------------
        # Determine missing mappings.
        # --------------------------------------------------------

        missing_before = [
            pair
            for pair in FOREX_PAIRS
            if stable_api.OP_code.ACTIVES.get(pair) is None
        ]

        if not missing_before and not force:

            logger.info(
                "All real Forex asset IDs are already available."
            )

            return True

        if missing_before:

            logger.info(
                "Missing real Forex mappings: %s",
                ", ".join(missing_before),
            )

        else:

            logger.info(
                "Forcing real Forex asset mapping refresh..."
            )

        # --------------------------------------------------------
        # Request initialization data.
        # --------------------------------------------------------

        try:

            API.api.api_option_init_all_result = None

            start_time = time.time()

            logger.info(
                "Requesting IQ Option asset initialization..."
            )

            API.api.get_api_option_init_all()

            # ----------------------------------------------------
            # Bounded wait.
            # ----------------------------------------------------

            while (
                API.api.api_option_init_all_result is None
                and time.time() - start_time
                < ASSET_REFRESH_TIMEOUT
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

            # ----------------------------------------------------
            # Validate response.
            # ----------------------------------------------------

            if not result.get(
                "isSuccessful",
                False,
            ):

                logger.error(
                    "IQ Option returned unsuccessful asset initialization."
                )

                return False

            result_data = result.get(
                "result",
                {},
            )

            found = {}

            # ----------------------------------------------------
            # Search binary + turbo.
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

                logger.info(
                    "Searching %s market actives: %d",
                    market_type.upper(),
                    len(actives),
                )

                for active_id, active_data in actives.items():

                    try:

                        raw_name = active_data.get(
                            "name",
                            "",
                        )

                        asset_name = normalize_asset_name(
                            raw_name
                        )

                        if asset_name is None:
                            continue

                        numeric_id = int(active_id)

                        # ------------------------------------------------
                        # Store exactly where iqoptionapi.get_candles()
                        # expects to find the active ID.
                        # ------------------------------------------------

                        stable_api.OP_code.ACTIVES[
                            asset_name
                        ] = numeric_id

                        found[asset_name] = numeric_id

                    except Exception as exc:

                        logger.debug(
                            "Could not process active %s: %s",
                            active_id,
                            exc,
                        )

            # --------------------------------------------------------
            # Display mapping results.
            # --------------------------------------------------------

            logger.info("")
            logger.info(
                "CURRENT REAL FOREX ASSET MAPPINGS"
            )
            logger.info("-" * 75)

            all_found = True

            for pair in FOREX_PAIRS:

                active_id = stable_api.OP_code.ACTIVES.get(
                    pair
                )

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

            logger.info("-" * 75)

            if all_found:

                logger.info(
                    "All %d real Forex pairs successfully mapped.",
                    len(FOREX_PAIRS),
                )

            else:

                missing_after = [
                    pair
                    for pair in FOREX_PAIRS
                    if stable_api.OP_code.ACTIVES.get(pair)
                    is None
                ]

                logger.warning(
                    "Still missing: %s",
                    ", ".join(missing_after),
                )

            return all_found

        except Exception as exc:

            logger.exception(
                "Real Forex asset refresh failed: %s",
                exc,
            )

            return False


# ============================================================
# VERIFY REQUIRED ASSETS
# ============================================================

def verify_required_assets():
    """
    Verify that every required real Forex pair has an
    active ID in the runtime ACTIVES dictionary.
    """

    logger.info("")
    logger.info("=" * 75)
    logger.info("VERIFYING REQUIRED REAL FOREX ASSETS")
    logger.info("=" * 75)

    missing = []

    for pair in FOREX_PAIRS:

        active_id = stable_api.OP_code.ACTIVES.get(
            pair
        )

        if active_id is None:

            logger.error(
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

    logger.info("-" * 75)

    if missing:

        logger.error(
            "Missing real Forex assets: %s",
            ", ".join(missing),
        )

        return False

    logger.info(
        "All %d real Forex pairs are ready.",
        len(FOREX_PAIRS),
    )

    return True


# ============================================================
# GET COMPLETED CANDLES
# ============================================================

def get_completed_candles(pair):
    """
    Retrieve recent completed 1-minute candles.

    The currently forming candle is excluded.

    Returns:
        list of completed candles
    """

    global API

    try:

        # --------------------------------------------------------
        # Verify mapping.
        # --------------------------------------------------------

        active_id = stable_api.OP_code.ACTIVES.get(
            pair
        )

        if active_id is None:

            logger.warning(
                "%s has no active ID.",
                pair,
            )

            return []

        # --------------------------------------------------------
        # Use IQ Option server time.
        # --------------------------------------------------------

        end_time = get_server_time()

        # --------------------------------------------------------
        # Request candles.
        # --------------------------------------------------------

        candles = API.get_candles(
            pair,
            TIMEFRAME,
            CANDLE_COUNT,
            end_time,
        )

        if not candles:
            return []

        completed = []

        # --------------------------------------------------------
        # Filter out currently forming candle.
        # --------------------------------------------------------

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

            # A candle is completed only if its ending
            # timestamp has already passed.
            if candle_to <= end_time:

                completed.append(candle)

        # --------------------------------------------------------
        # Chronological order.
        # --------------------------------------------------------

        completed.sort(
            key=lambda candle: float(
                candle["from"]
            )
        )

        return completed[-CANDLE_COUNT:]

    except Exception as exc:

        logger.error(
            "%s candle error: %s",
            pair,
            exc,
        )

        return []


# ============================================================
# CANDLE COLOR
# ============================================================

def candle_color(candle):
    """
    Determine candle direction.

    Returns:

        GREEN
        RED
        DOJI
    """

    try:

        open_price = float(
            candle["open"]
        )

        close_price = float(
            candle["close"]
        )

    except Exception:

        return "DOJI"

    if close_price > open_price:
        return "GREEN"

    if close_price < open_price:
        return "RED"

    return "DOJI"


# ============================================================
# STREAK ANALYSIS
# ============================================================

def analyze_streak(candles):
    """
    Analyze the most recent completed candles.

    A DOJI breaks the streak.

    Returns:

        color
        streak_count
        last_candle
    """

    if not candles:
        return None, 0, None

    last_candle = candles[-1]

    last_color = candle_color(
        last_candle
    )

    if last_color == "DOJI":

        return (
            "DOJI",
            0,
            last_candle,
        )

    streak = 0

    for candle in reversed(candles):

        color = candle_color(
            candle
        )

        if color != last_color:
            break

        streak += 1

    return (
        last_color,
        streak,
        last_candle,
    )


# ============================================================
# BUILD SIGNAL
# ============================================================

def build_signal(
    pair,
    color,
    streak,
    candle,
):
    """
    Build the Telegram reversal signal.
    """

    candle_time = float(
        candle["from"]
    )

    # The next candle is the intended entry candle.
    entry_time = (
        candle_time + TIMEFRAME
    )

    if color == "GREEN":

        prediction = "REVERSAL DOWN 🟥"
        direction = "LOWER / PUT 🟥"

    else:

        prediction = "REVERSAL UP 🟩"
        direction = "HIGHER / CALL 🟩"

    # --------------------------------------------------------
    # Martingale timing schedule.
    #
    # These are signal timing levels only.
    # The bot does NOT execute trades.
    # --------------------------------------------------------

    level_1 = entry_time + 60
    level_2 = entry_time + 120
    level_3 = entry_time + 180

    message = (
        "🚨 <b>REAL FOREX BINARY SIGNAL</b>\n"
        "\n"
        f"📊 <b>Pair:</b> {pair}\n"
        f"💱 <b>Market:</b> REAL FOREX\n"
        f"📜 <b>Contract:</b> BINARY\n"
        f"⏳ <b>Timeframe:</b> 1 Minute\n"
        f"🔥 <b>Streak:</b> {streak} {color} candles\n"
        f"🔮 <b>Prediction:</b> {prediction}\n"
        f"📈 <b>Direction:</b> {direction}\n"
        "\n"
        f"🕐 <b>Entry:</b> {format_time(entry_time)}\n"
        "\n"
        "💰 <b>Martingale Schedule</b>\n"
        f"Level 1: {format_time(level_1)}\n"
        f"Level 2: {format_time(level_2)}\n"
        f"Level 3: {format_time(level_3)}\n"
        "\n"
        f"🕯 <b>Signal candle:</b> "
        f"{format_time(candle_time)}\n"
        "\n"
        "⚠️ <b>Mode:</b> SIGNAL ONLY"
    )

    return message


# ============================================================
# PROCESS ONE PAIR
# ============================================================

def process_pair(pair):
    """
    Analyze one real Forex pair.
    """

    try:

        candles = get_completed_candles(
            pair
        )

        if len(candles) < MIN_STREAK:

            logger.debug(
                "%-12s | Not enough candles: %d",
                pair,
                len(candles),
            )

            return

        color, streak, last_candle = analyze_streak(
            candles
        )

        if color not in (
            "GREEN",
            "RED",
        ):

            return

        logger.info(
            "%-12s | %s streak = %d",
            pair,
            color,
            streak,
        )

        # --------------------------------------------------------
        # Signal specifically when the streak reaches seven.
        # --------------------------------------------------------

        if streak != MIN_STREAK:
            return

        candle_id = int(
            float(
                last_candle["from"]
            )
        )

        # --------------------------------------------------------
        # Prevent duplicate alerts.
        # --------------------------------------------------------

        if LAST_SIGNAL_CANDLE.get(pair) == candle_id:

            logger.debug(
                "%s | Signal already sent for candle %s",
                pair,
                candle_id,
            )

            return

        # --------------------------------------------------------
        # Build signal.
        # --------------------------------------------------------

        message = build_signal(
            pair,
            color,
            streak,
            last_candle,
        )

        # --------------------------------------------------------
        # Send Telegram.
        # --------------------------------------------------------

        sent = send_telegram(
            message
        )

        if sent:

            LAST_SIGNAL_CANDLE[pair] = candle_id

            logger.info(
                "SIGNAL GENERATED: %s | %s | streak=%d",
                pair,
                color,
                streak,
            )

    except Exception as exc:

        logger.exception(
            "Error processing %s: %s",
            pair,
            exc,
        )


# ============================================================
# SCAN ALL PAIRS
# ============================================================

def scan_all_pairs():
    """
    Sequentially scan all real Forex pairs.

    Sequential scanning is intentional.

    The older iqoptionapi implementation can behave poorly
    when many websocket candle requests are sent simultaneously.
    """

    for pair in FOREX_PAIRS:

        process_pair(
            pair
        )


# ============================================================
# REFRESH MAPPINGS AFTER CONNECTION LOSS
# ============================================================

def recover_connection_and_assets():
    """
    Reconnect and rebuild real Forex asset mappings.

    Returns:
        True if both connection and mappings are ready.
    """

    logger.warning(
        "Starting connection/asset recovery..."
    )

    # --------------------------------------------------------
    # Reconnect.
    # --------------------------------------------------------

    connect_iq_option()

    if not connection_alive():

        logger.error(
            "Connection recovery failed."
        )

        return False

    # --------------------------------------------------------
    # Force a fresh asset initialization request.
    # --------------------------------------------------------

    if not refresh_forex_asset_mappings(
        force=True
    ):

        logger.error(
            "Real Forex asset mapping recovery failed."
        )

        return False

    # --------------------------------------------------------
    # Verify.
    # --------------------------------------------------------

    if not verify_required_assets():

        logger.error(
            "Real Forex asset verification failed after recovery."
        )

        return False

    logger.info(
        "Connection and real Forex mappings successfully recovered."
    )

    return True


# ============================================================
# SEND STARTUP MESSAGE
# ============================================================

def send_startup_message():
    """
    Send Telegram startup notification.
    """

    pairs = ", ".join(
        FOREX_PAIRS
    )

    message = (
        "🤖 <b>FIREFLY AI BINARY SCANNER STARTED</b>\n"
        "\n"
        "💱 <b>Market:</b> REAL FOREX\n"
        "📜 <b>Contract:</b> BINARY\n"
        "⏱ <b>Timeframe:</b> 1 Minute\n"
        "🔥 <b>Strategy:</b> 7-candle reversal\n"
        "📡 <b>Mode:</b> SIGNAL ONLY\n"
        "\n"
        f"📊 <b>Pairs:</b>\n"
        f"{pairs}\n"
        "\n"
        "⚠️ OTC symbols are NOT being used."
    )

    send_telegram(
        message
    )


# ============================================================
# MAIN BOT LOOP
# ============================================================

def run_bot():
    """
    Main Firefly AI binary scanner.
    """

    global API

    # --------------------------------------------------------
    # Startup banner.
    # --------------------------------------------------------

    logger.info("")
    logger.info("=" * 75)
    logger.info(
        "FIREFLY AI - REAL FOREX BINARY REVERSAL SCANNER"
    )
    logger.info("=" * 75)

    logger.info(
        "Market: REAL FOREX"
    )

    logger.info(
        "Contract: BINARY"
    )

    logger.info(
        "Pairs: %d",
        len(FOREX_PAIRS),
    )

    logger.info(
        "Timeframe: 1 minute"
    )

    logger.info(
        "Candles: %d",
        CANDLE_COUNT,
    )

    logger.info(
        "Required streak: %d",
        MIN_STREAK,
    )

    logger.info(
        "Polling interval: %d seconds",
        POLL_INTERVAL,
    )

    logger.info(
        "Timezone: Nigeria / UTC+1"
    )

    logger.info("=" * 75)

    # --------------------------------------------------------
    # CONNECT.
    # --------------------------------------------------------

    if not connect_iq_option():

        logger.error(
            "Unable to establish IQ Option connection."
        )

        return

    # --------------------------------------------------------
    # Build mappings.
    # --------------------------------------------------------

    if not refresh_forex_asset_mappings(
        force=True
    ):

        logger.error(
            "Could not obtain all required real Forex asset IDs."
        )

        logger.error(
            "The bot will not start with incomplete mappings."
        )

        return

    # --------------------------------------------------------
    # Verify mappings.
    # --------------------------------------------------------

    if not verify_required_assets():

        logger.error(
            "Required real Forex assets are missing."
        )

        return

    # --------------------------------------------------------
    # Startup.
    # --------------------------------------------------------

    logger.info("")
    logger.info("=" * 75)
    logger.info(
        "REAL FOREX BINARY SCANNER STARTED"
    )
    logger.info("=" * 75)

    send_startup_message()

    # --------------------------------------------------------
    # Main scanner.
    # --------------------------------------------------------

    while True:

        try:

            # ====================================================
            # CONNECTION CHECK
            # ====================================================

            if not connection_alive():

                logger.warning(
                    "IQ Option connection lost."
                )

                if not recover_connection_and_assets():

                    logger.warning(
                        "Recovery unsuccessful. "
                        "Retrying in %d seconds...",
                        RECONNECT_DELAY,
                    )

                    time.sleep(
                        RECONNECT_DELAY
                    )

                    continue

            # ====================================================
            # PERIODIC ASSET VALIDATION
            # ====================================================
            #
            # A websocket/session can remain technically alive
            # while its active mapping state becomes incomplete.
            #
            # Therefore check the mappings before scanning.
            # ====================================================

            missing_assets = [
                pair
                for pair in FOREX_PAIRS
                if stable_api.OP_code.ACTIVES.get(pair) is None
            ]

            if missing_assets:

                logger.warning(
                    "Missing real Forex mappings detected: %s",
                    ", ".join(missing_assets),
                )

                if not refresh_forex_asset_mappings(
                    force=True
                ):

                    logger.warning(
                        "Mapping refresh unsuccessful."
                    )

                    time.sleep(
                        RECONNECT_DELAY
                    )

                    continue

                if not verify_required_assets():

                    logger.warning(
                        "Mappings still incomplete."
                    )

                    time.sleep(
                        RECONNECT_DELAY
                    )

                    continue

            # ====================================================
            # SCAN
            # ====================================================

            cycle_start = time.time()

            logger.info("")
            logger.info(
                "========== BINARY SCAN CYCLE =========="
            )

            scan_all_pairs()

            elapsed = (
                time.time()
                - cycle_start
            )

            logger.info(
                "Scan completed in %.2f seconds.",
                elapsed,
            )

            # ====================================================
            # WAIT
            # ====================================================

            sleep_time = max(
                1,
                int(
                    POLL_INTERVAL
                    - elapsed
                ),
            )

            logger.info(
                "Next scan in %d seconds...",
                sleep_time,
            )

            time.sleep(
                sleep_time
            )

        # ========================================================
        # USER STOP
        # ========================================================

        except KeyboardInterrupt:

            logger.info("")
            logger.info(
                "Binary scanner stopped by user."
            )

            break

        # ========================================================
        # UNEXPECTED ERROR
        # ========================================================

        except Exception as exc:

            logger.exception(
                "Main loop error: %s",
                exc,
            )

            logger.info(
                "Attempting recovery..."
            )

            time.sleep(
                RECONNECT_DELAY
            )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    try:

        run_bot()

    except KeyboardInterrupt:

        print(
            "\nBinary bot stopped."
        )

    except Exception as exc:

        logger.exception(
            "Fatal error: %s",
            exc,
        )

