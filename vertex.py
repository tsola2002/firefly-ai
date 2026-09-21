import os
import sys
import time
import logging
import threading
from datetime import datetime

import requests
import pandas as pd
from dotenv import load_dotenv

from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.stable_api as stable_api


# ============================================================
# CONFIGURATION
# ============================================================

load_dotenv()


IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ------------------------------------------------------------
# MARKET SETTINGS
# ------------------------------------------------------------

TIMEFRAME = 60

# We need enough candles for:
# - MA 100
# - MACD 22
# - Vortex 14
# - Supertrend 10
#
# 200 gives the indicators enough warm-up data.
CANDLE_COUNT = 200

POLL_INTERVAL = 15

ASSET_REFRESH_TIMEOUT = 15

RECONNECT_DELAY = 5


# ------------------------------------------------------------
# STRATEGY SETTINGS
# ------------------------------------------------------------

MACD_FAST = 10
MACD_SLOW = 22
MACD_SIGNAL = 9

VORTEX_PERIOD = 14

SUPERTREND_ATR_PERIOD = 10
SUPERTREND_MULTIPLIER = 3.0

MA_PERIOD = 100

TRADE_EXPIRATION_MINUTES = 5


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

logger = logging.getLogger("OTC-5MIN-VORTEX-BOT")


# ============================================================
# GLOBAL STATE
# ============================================================

API = None

# Prevent duplicate signals for the same pair/candle.
LAST_SIGNAL_CANDLE = {}

# Prevent multiple asset-refresh operations.
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
    Windows-safe time formatting.
    """

    return datetime.fromtimestamp(
        timestamp
    ).strftime(
        "%I:%M %p"
    ).lstrip("0")


# ============================================================
# SERVER TIME
# ============================================================

def get_server_time():
    """
    Attempt to use IQ Option server timestamp.

    Falls back to local Unix time if unavailable.
    """

    try:

        server_timestamp = (
            API.api.timesync.server_timestamp
        )

        if server_timestamp:

            return float(
                server_timestamp
            )

    except Exception:

        pass

    return time.time()


# ============================================================
# CONNECTION
# ============================================================

def connect_iq_option():
    """
    Establish the IQ Option connection.
    """

    global API

    while True:

        try:

            logger.info("=" * 70)
            logger.info("CONNECTING TO IQ OPTION")
            logger.info("=" * 70)

            API = IQ_Option(
                IQ_EMAIL,
                IQ_PASSWORD,
            )

            check, reason = API.connect()

            if check:

                logger.info(
                    "IQ Option connection successful."
                )

                try:

                    API.change_balance(
                        "PRACTICE"
                    )

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
            "Retrying connection in %s seconds...",
            RECONNECT_DELAY,
        )

        time.sleep(
            RECONNECT_DELAY
        )


# ============================================================
# CONNECTION CHECK
# ============================================================

def connection_alive():

    try:

        return API.check_connect()

    except Exception:

        return False


# ============================================================
# DYNAMIC OTC ASSET MAPPING
# ============================================================

def refresh_otc_asset_mappings():

    global API

    with ASSET_LOCK:

        logger.info("=" * 70)
        logger.info("REFRESHING OTC ASSET MAPPINGS")
        logger.info("=" * 70)

        missing_before = [
            pair
            for pair in OTC_PAIRS
            if stable_api.OP_code.ACTIVES.get(pair) is None
        ]

        if not missing_before:

            logger.info(
                "All OTC asset IDs are already available."
            )

            return True

        logger.info(
            "Missing OTC mappings: %s",
            ", ".join(missing_before),
        )

        try:

            API.api.api_option_init_all_result = None

            start_time = time.time()

            logger.info(
                "Requesting binary/turbo initialization data..."
            )

            API.api.get_api_option_init_all()

            while (
                API.api.api_option_init_all_result is None
                and time.time() - start_time
                < ASSET_REFRESH_TIMEOUT
            ):

                time.sleep(0.1)

            result = (
                API.api.api_option_init_all_result
            )

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

            # ------------------------------------------------
            # BINARY + TURBO
            # ------------------------------------------------

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

                            numeric_id = int(
                                active_id
                            )

                            stable_api.OP_code.ACTIVES[
                                asset_name
                            ] = numeric_id

                    except Exception as exc:

                        logger.debug(
                            "Could not process active %s: %s",
                            active_id,
                            exc,
                        )

            # ------------------------------------------------
            # DISPLAY MAPPINGS
            # ------------------------------------------------

            logger.info("")
            logger.info(
                "CURRENT OTC ASSET MAPPINGS"
            )
            logger.info("-" * 70)

            all_found = True

            for pair in OTC_PAIRS:

                active_id = (
                    stable_api.OP_code.ACTIVES.get(
                        pair
                    )
                )

                if active_id is not None:

                    logger.info(
                        "  %-15s -> %s",
                        pair,
                        active_id,
                    )

                else:

                    logger.error(
                        "  %-15s -> NOT FOUND",
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
# VERIFY REQUIRED ASSETS
# ============================================================

def verify_required_assets():

    logger.info("")
    logger.info(
        "VERIFYING REQUIRED OTC ASSETS"
    )
    logger.info("-" * 70)

    missing = []

    for pair in OTC_PAIRS:

        active_id = (
            stable_api.OP_code.ACTIVES.get(
                pair
            )
        )

        if active_id is None:

            logger.error(
                "MISSING: %s",
                pair,
            )

            missing.append(pair)

        else:

            logger.info(
                "READY:   %-15s -> %s",
                pair,
                active_id,
            )

    logger.info("-" * 70)

    if missing:

        logger.error(
            "Missing assets: %s",
            ", ".join(missing),
        )

        return False

    logger.info(
        "All 8 OTC pairs are ready."
    )

    return True


# ============================================================
# GET COMPLETED CANDLES
# ============================================================

def get_completed_candles(pair):

    try:

        if (
            stable_api.OP_code.ACTIVES.get(
                pair
            ) is None
        ):

            logger.warning(
                "%s has no active ID.",
                pair,
            )

            return []

        end_time = get_server_time()

        candles = API.get_candles(
            pair,
            TIMEFRAME,
            CANDLE_COUNT,
            end_time,
        )

        if not candles:

            return []

        completed = []

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

            if candle_to <= end_time:

                completed.append(
                    candle
                )

        completed.sort(
            key=lambda x: float(
                x["from"]
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
# CONVERT CANDLES TO DATAFRAME
# ============================================================

def candles_to_dataframe(candles):

    rows = []

    for candle in candles:

        rows.append(
            {
                "timestamp": float(
                    candle["from"]
                ),
                "open": float(
                    candle["open"]
                ),
                "high": float(
                    candle["max"]
                ),
                "low": float(
                    candle["min"]
                ),
                "close": float(
                    candle["close"]
                ),
            }
        )

    df = pd.DataFrame(rows)

    if df.empty:

        return df

    df = df.sort_values(
        "timestamp"
    ).reset_index(
        drop=True
    )

    return df


# ============================================================
# EMA
# ============================================================

def calculate_ema(series, period):

    return series.ewm(
        span=period,
        adjust=False,
    ).mean()


# ============================================================
# MACD
# ============================================================

def calculate_macd(df):

    fast_ema = calculate_ema(
        df["close"],
        MACD_FAST,
    )

    slow_ema = calculate_ema(
        df["close"],
        MACD_SLOW,
    )

    df["macd"] = (
        fast_ema - slow_ema
    )

    df["macd_signal"] = calculate_ema(
        df["macd"],
        MACD_SIGNAL,
    )

    df["macd_hist"] = (
        df["macd"]
        - df["macd_signal"]
    )

    return df


# ============================================================
# VORTEX INDICATOR
# ============================================================

def calculate_vortex(df):

    high = df["high"]
    low = df["low"]
    close = df["close"]

    previous_high = high.shift(1)
    previous_low = low.shift(1)
    previous_close = close.shift(1)

    # True Range
    tr1 = high - low

    tr2 = (
        high - previous_close
    ).abs()

    tr3 = (
        low - previous_close
    ).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1,
    ).max(
        axis=1
    )

    # Positive / negative vortex movement
    vm_plus = (
        high - previous_low
    ).abs()

    vm_minus = (
        low - previous_high
    ).abs()

    tr_sum = (
        true_range
        .rolling(
            VORTEX_PERIOD
        )
        .sum()
    )

    vm_plus_sum = (
        vm_plus
        .rolling(
            VORTEX_PERIOD
        )
        .sum()
    )

    vm_minus_sum = (
        vm_minus
        .rolling(
            VORTEX_PERIOD
        )
        .sum()
    )

    df["vi_plus"] = (
        vm_plus_sum
        / tr_sum
    )

    df["vi_minus"] = (
        vm_minus_sum
        / tr_sum
    )

    return df


# ============================================================
# ATR
# ============================================================

def calculate_atr(df, period):

    high = df["high"]
    low = df["low"]
    close = df["close"]

    previous_close = close.shift(1)

    tr1 = high - low

    tr2 = (
        high - previous_close
    ).abs()

    tr3 = (
        low - previous_close
    ).abs()

    true_range = pd.concat(
        [tr1, tr2, tr3],
        axis=1,
    ).max(
        axis=1
    )

    return (
        true_range
        .rolling(period)
        .mean()
    )


# ============================================================
# SUPERTREND
# ============================================================

def calculate_supertrend(df):

    period = SUPERTREND_ATR_PERIOD
    multiplier = SUPERTREND_MULTIPLIER

    atr = calculate_atr(
        df,
        period,
    )

    hl2 = (
        df["high"]
        + df["low"]
    ) / 2

    basic_upper = (
        hl2
        + multiplier * atr
    )

    basic_lower = (
        hl2
        - multiplier * atr
    )

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()

    supertrend = pd.Series(
        index=df.index,
        dtype=float,
    )

    direction = pd.Series(
        index=df.index,
        dtype=int,
    )

    for i in range(len(df)):

        if i == 0:

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

            supertrend.iloc[i] = (
                final_upper.iloc[i]
            )

            direction.iloc[i] = -1

            continue

        # Final upper band
        if (
            basic_upper.iloc[i]
            < final_upper.iloc[i - 1]
            or df["close"].iloc[i - 1]
            > final_upper.iloc[i - 1]
        ):

            final_upper.iloc[i] = (
                basic_upper.iloc[i]
            )

        else:

            final_upper.iloc[i] = (
                final_upper.iloc[i - 1]
            )

        # Final lower band
        if (
            basic_lower.iloc[i]
            > final_lower.iloc[i - 1]
            or df["close"].iloc[i - 1]
            < final_lower.iloc[i - 1]
        ):

            final_lower.iloc[i] = (
                basic_lower.iloc[i]
            )

        else:

            final_lower.iloc[i] = (
                final_lower.iloc[i - 1]
            )

        # Supertrend direction
        if (
            supertrend.iloc[i - 1]
            == final_upper.iloc[i - 1]
        ):

            if (
                df["close"].iloc[i]
                <= final_upper.iloc[i]
            ):

                supertrend.iloc[i] = (
                    final_upper.iloc[i]
                )

                direction.iloc[i] = -1

            else:

                supertrend.iloc[i] = (
                    final_lower.iloc[i]
                )

                direction.iloc[i] = 1

        else:

            if (
                df["close"].iloc[i]
                >= final_lower.iloc[i]
            ):

                supertrend.iloc[i] = (
                    final_lower.iloc[i]
                )

                direction.iloc[i] = 1

            else:

                supertrend.iloc[i] = (
                    final_upper.iloc[i]
                )

                direction.iloc[i] = -1

    df["supertrend"] = supertrend
    df["supertrend_direction"] = direction

    return df


# ============================================================
# 100 MA
# ============================================================

def calculate_ma(df):

    df["ma100"] = (
        df["close"]
        .rolling(
            MA_PERIOD
        )
        .mean()
    )

    df["ma100_previous"] = (
        df["ma100"]
        .shift(1)
    )

    return df


# ============================================================
# CALCULATE ALL INDICATORS
# ============================================================

def calculate_indicators(df):

    df = calculate_macd(df)

    df = calculate_vortex(df)

    df = calculate_supertrend(df)

    df = calculate_ma(df)

    return df


# ============================================================
# MACD CROSSOVER
# ============================================================

def macd_bullish_crossover(df):

    if len(df) < 2:

        return False

    previous = df.iloc[-2]
    current = df.iloc[-1]

    return (
        previous["macd"]
        <= previous["macd_signal"]
        and
        current["macd"]
        > current["macd_signal"]
    )


def macd_bearish_crossover(df):

    if len(df) < 2:

        return False

    previous = df.iloc[-2]
    current = df.iloc[-1]

    return (
        previous["macd"]
        >= previous["macd_signal"]
        and
        current["macd"]
        < current["macd_signal"]
    )


# ============================================================
# BULLISH INDICATOR CHECK
# ============================================================

def check_bullish_conditions(df):

    if len(df) < MA_PERIOD + 5:

        return False

    current = df.iloc[-1]

    macd_condition = (
        macd_bullish_crossover(df)
    )

    vortex_condition = (
        current["vi_plus"]
        > current["vi_minus"]
    )

    supertrend_condition = (
        current["close"]
        > current["supertrend"]
        and
        current["supertrend_direction"]
        == 1
    )

    ma_condition = (
        current["close"]
        > current["ma100"]
        and
        current["ma100"]
        > current["ma100_previous"]
    )

    return (
        macd_condition
        and vortex_condition
        and supertrend_condition
        and ma_condition
    )


# ============================================================
# BEARISH INDICATOR CHECK
# ============================================================

def check_bearish_conditions(df):

    if len(df) < MA_PERIOD + 5:

        return False

    current = df.iloc[-1]

    macd_condition = (
        macd_bearish_crossover(df)
    )

    vortex_condition = (
        current["vi_minus"]
        > current["vi_plus"]
    )

    supertrend_condition = (
        current["close"]
        < current["supertrend"]
        and
        current["supertrend_direction"]
        == -1
    )

    ma_condition = (
        current["close"]
        < current["ma100"]
        and
        current["ma100"]
        < current["ma100_previous"]
    )

    return (
        macd_condition
        and vortex_condition
        and supertrend_condition
        and ma_condition
    )


# ============================================================
# INDICATOR STATUS
# ============================================================

def get_indicator_status(df):

    current = df.iloc[-1]

    return {
        "macd": (
            "BULLISH"
            if current["macd"]
            > current["macd_signal"]
            else "BEARISH"
        ),

        "vortex": (
            "BULLISH"
            if current["vi_plus"]
            > current["vi_minus"]
            else "BEARISH"
        ),

        "supertrend": (
            "BULLISH"
            if current["supertrend_direction"] == 1
            else "BEARISH"
        ),

        "ma100": (
            "BULLISH"
            if (
                current["close"]
                > current["ma100"]
                and
                current["ma100"]
                > current["ma100_previous"]
            )
            else "BEARISH"
        ),
    }


# ============================================================
# BUILD SIGNAL
# ============================================================

def build_signal(
    pair,
    direction,
    candle,
    df,
):

    candle_time = float(
        candle["from"]
    )

    entry_time = (
        candle_time
        + TIMEFRAME
    )

    expiry_time = (
        entry_time
        + (
            TRADE_EXPIRATION_MINUTES
            * 60
        )
    )

    current = df.iloc[-1]

    status = get_indicator_status(
        df
    )

    if direction == "CALL":

        direction_icon = "🟢"
        prediction = "BULLISH"

    else:

        direction_icon = "🔴"
        prediction = "BEARISH"

    message = (
        "🚨 <b>5-MINUTE VORTEX SIGNAL</b>\n"
        "\n"
        f"📊 <b>Pair:</b> {pair}\n"
        f"⏱ <b>Timeframe:</b> 1 Minute\n"
        f"⌛ <b>Expiration:</b> 5 Minutes\n"
        f"🔮 <b>Prediction:</b> {prediction}\n"
        f"🎯 <b>Direction:</b> "
        f"{direction_icon} {direction}\n"
        "\n"
        "📋 <b>INDICATOR CONFIRMATION</b>\n"
        f"📈 MACD 10/22/9: "
        f"<b>{status['macd']}</b>\n"
        f"🌪 Vortex 14: "
        f"<b>{status['vortex']}</b>\n"
        f"📐 Supertrend 10/3: "
        f"<b>{status['supertrend']}</b>\n"
        f"📊 MA 100: "
        f"<b>{status['ma100']}</b>\n"
        "\n"
        "✅ <b>ALL 4 INDICATORS CONFIRMED</b>\n"
        "\n"
        f"🕐 <b>Entry:</b> "
        f"{format_time(entry_time)}\n"
        f"🏁 <b>Expiry:</b> "
        f"{format_time(expiry_time)}\n"
        "\n"
        f"💵 <b>Close:</b> "
        f"{current['close']:.6f}\n"
        f"📈 <b>MA100:</b> "
        f"{current['ma100']:.6f}\n"
        f"🌪 <b>VI+:</b> "
        f"{current['vi_plus']:.4f}\n"
        f"🌪 <b>VI-:</b> "
        f"{current['vi_minus']:.4f}\n"
        "\n"
        f"🕯 <b>Signal Candle:</b> "
        f"{format_time(candle_time)}\n"
    )

    return message


# ============================================================
# PROCESS ONE PAIR
# ============================================================

def process_pair(pair):

    try:

        candles = get_completed_candles(
            pair
        )

        if len(candles) < MA_PERIOD + 5:

            logger.warning(
                "%s | Not enough candles: %d",
                pair,
                len(candles),
            )

            return

        df = candles_to_dataframe(
            candles
        )

        if df.empty:

            return

        df = calculate_indicators(
            df
        )

        # ----------------------------------------------------
        # Remove rows where indicators are not ready.
        # ----------------------------------------------------

        required_columns = [
            "macd",
            "macd_signal",
            "vi_plus",
            "vi_minus",
            "supertrend",
            "ma100",
            "ma100_previous",
        ]

        df = df.dropna(
            subset=required_columns
        )

        if len(df) < 2:

            return

        last_candle = candles[-1]

        candle_id = int(
            float(
                last_candle["from"]
            )
        )

        current = df.iloc[-1]

        bullish = check_bullish_conditions(
            df
        )

        bearish = check_bearish_conditions(
            df
        )

        logger.info(
            "%-15s | Close %.6f | "
            "MACD %.6f/%.6f | "
            "VI %.3f/%.3f | "
            "ST %s | "
            "MA %.6f",
            pair,
            current["close"],
            current["macd"],
            current["macd_signal"],
            current["vi_plus"],
            current["vi_minus"],
            (
                "BULL"
                if current["supertrend_direction"] == 1
                else "BEAR"
            ),
            current["ma100"],
        )

        # ----------------------------------------------------
        # No complete signal.
        # ----------------------------------------------------

        if not bullish and not bearish:

            return

        # ----------------------------------------------------
        # Prevent duplicate alert.
        # ----------------------------------------------------

        if (
            LAST_SIGNAL_CANDLE.get(pair)
            == candle_id
        ):

            logger.info(
                "%s | Signal already sent for candle %s",
                pair,
                candle_id,
            )

            return

        # ----------------------------------------------------
        # Determine direction.
        # ----------------------------------------------------

        if bullish:

            direction = "CALL"

        elif bearish:

            direction = "PUT"

        else:

            return

        # ----------------------------------------------------
        # Build message.
        # ----------------------------------------------------

        message = build_signal(
            pair,
            direction,
            last_candle,
            df,
        )

        # ----------------------------------------------------
        # Send Telegram.
        # ----------------------------------------------------

        sent = send_telegram(
            message
        )

        if sent:

            LAST_SIGNAL_CANDLE[
                pair
            ] = candle_id

            logger.info(
                "================================================"
            )

            logger.info(
                "SIGNAL GENERATED"
            )

            logger.info(
                "PAIR: %s",
                pair,
            )

            logger.info(
                "DIRECTION: %s",
                direction,
            )

            logger.info(
                "EXPIRATION: %d MINUTES",
                TRADE_EXPIRATION_MINUTES,
            )

            logger.info(
                "================================================"
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

    for pair in OTC_PAIRS:

        process_pair(
            pair
        )


# ============================================================
# MAIN BOT LOOP
# ============================================================

def run_bot():

    global API

    logger.info("")
    logger.info("=" * 70)
    logger.info(
        "IQ OPTION 5-MINUTE VORTEX SCANNER"
    )
    logger.info("=" * 70)

    logger.info(
        "Pairs: %d",
        len(OTC_PAIRS),
    )

    logger.info(
        "Chart timeframe: 1 minute"
    )

    logger.info(
        "Trade expiration: 5 minutes"
    )

    logger.info(
        "MACD: %d/%d/%d",
        MACD_FAST,
        MACD_SLOW,
        MACD_SIGNAL,
    )

    logger.info(
        "Vortex: %d",
        VORTEX_PERIOD,
    )

    logger.info(
        "Supertrend: ATR %d / Multiplier %.1f",
        SUPERTREND_ATR_PERIOD,
        SUPERTREND_MULTIPLIER,
    )

    logger.info(
        "MA: %d",
        MA_PERIOD,
    )

    logger.info(
        "Required confirmations: 4/4"
    )

    logger.info("=" * 70)

    # --------------------------------------------------------
    # CONNECT
    # --------------------------------------------------------

    if not connect_iq_option():

        logger.error(
            "Unable to establish IQ Option connection."
        )

        return

    # --------------------------------------------------------
    # REFRESH ASSETS
    # --------------------------------------------------------

    if not refresh_otc_asset_mappings():

        logger.error(
            "Could not obtain all required OTC asset IDs."
        )

        return

    # --------------------------------------------------------
    # VERIFY
    # --------------------------------------------------------

    if not verify_required_assets():

        logger.error(
            "Required OTC assets are missing."
        )

        return

    # --------------------------------------------------------
    # START
    # --------------------------------------------------------

    logger.info("")
    logger.info("=" * 70)
    logger.info(
        "5-MINUTE VORTEX SCANNER STARTED"
    )
    logger.info("=" * 70)

    send_telegram(
        "🤖 <b>5-Minute Vortex Scanner Started</b>\n\n"
        "📊 Monitoring: 8 OTC pairs\n"
        "⏱ Chart: 1-minute candles\n"
        "⌛ Expiration: 5 minutes\n"
        "\n"
        "📋 <b>Indicators</b>\n"
        "• MACD 10/22/9\n"
        "• Vortex 14\n"
        "• Supertrend 10/3\n"
        "• MA 100\n"
        "\n"
        "✅ Signal requires all 4 indicators.\n"
        "📡 Mode: SIGNAL ONLY"
    )

    # --------------------------------------------------------
    # MAIN LOOP
    # --------------------------------------------------------

    while True:

        try:

            # ------------------------------------------------
            # CONNECTION CHECK
            # ------------------------------------------------

            if not connection_alive():

                logger.warning(
                    "IQ Option connection lost."
                )

                logger.info(
                    "Reconnecting..."
                )

                connect_iq_option()

                refresh_otc_asset_mappings()

                if not verify_required_assets():

                    logger.warning(
                        "Asset mapping incomplete after reconnect."
                    )

                    time.sleep(
                        RECONNECT_DELAY
                    )

                    continue

            # ------------------------------------------------
            # SCAN
            # ------------------------------------------------

            cycle_start = time.time()

            logger.info("")
            logger.info(
                "========== SCAN CYCLE =========="
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

            # ------------------------------------------------
            # WAIT
            # ------------------------------------------------

            sleep_time = max(
                1,
                POLL_INTERVAL - elapsed,
            )

            logger.info(
                "Next scan in %d seconds...",
                sleep_time,
            )

            time.sleep(
                sleep_time
            )

        except KeyboardInterrupt:

            logger.info("")
            logger.info(
                "Bot stopped by user."
            )

            break

        except Exception as exc:

            logger.exception(
                "Main loop error: %s",
                exc,
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
            "\nBot stopped."
        )

    except Exception as exc:

        logger.exception(
            "Fatal error: %s",
            exc,
        )