
"""
===============================================================================
FIREFLY AI - KELTNER CHANNEL + CCI MARKET SCANNER
===============================================================================

Strategy:
    Timeframe       : 2-minute candles (M2)

    Keltner Channel:
        EMA Length  : 25
        ATR Length  : 15
        ATR Mult    : 1.0

    CCI:
        Period      : 15
        BUY         : CCI > +100
        SELL        : CCI < -100

Signal Confirmation:
    BUY:
        Candle 1 closes above Upper Keltner Channel
        Candle 1 CCI > +100
        Candle 2 closes above Upper Keltner Channel
        Candle 2 CCI > +100
        -> BUY signal on Candle 2

    SELL:
        Candle 1 closes below Lower Keltner Channel
        Candle 1 CCI < -100
        Candle 2 closes below Lower Keltner Channel
        Candle 2 CCI < -100
        -> SELL signal on Candle 2

Features:
    - IQ Option API
    - PRACTICE account
    - 2-minute candles
    - Multiple OTC assets
    - Keltner Channels calculated locally
    - CCI calculated locally
    - Completed candles only
    - Duplicate signal protection
    - Breakout-cycle protection
    - Telegram notifications
    - Automatic reconnection
    - Rate-limit/network error handling
    - No automatic trades

===============================================================================
"""

import os
import sys
import time
import math
import logging
import traceback
import threading
from datetime import datetime, timezone, timedelta

import requests
import pandas as pd
import numpy as np
from dotenv import load_dotenv

from iqoptionapi.stable_api import IQ_Option


# =============================================================================
# ENVIRONMENT
# =============================================================================

load_dotenv()


# =============================================================================
# CONFIGURATION
# =============================================================================

# -----------------------------------------------------------------------------
# IQ OPTION
# -----------------------------------------------------------------------------

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

ACCOUNT_TYPE = "PRACTICE"


# -----------------------------------------------------------------------------
# TELEGRAM
# -----------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# -----------------------------------------------------------------------------
# MARKET SETTINGS
# -----------------------------------------------------------------------------

# IQ Option candle timeframe
TIMEFRAME_SECONDS = 120

# How often the scanner checks for newly closed candles
POLL_INTERVAL = 5

# Number of candles retrieved from IQ Option.
#
# We need enough history for:
#   EMA 25
#   ATR 15
#   CCI 15
#
# 100 gives us a comfortable warm-up period.
CANDLE_COUNT = 100


# -----------------------------------------------------------------------------
# KELTNER CHANNEL SETTINGS
# -----------------------------------------------------------------------------

KELTNER_EMA_PERIOD = 25
KELTNER_ATR_PERIOD = 15
KELTNER_MULTIPLIER = 1.0


# -----------------------------------------------------------------------------
# CCI SETTINGS
# -----------------------------------------------------------------------------

CCI_PERIOD = 15
CCI_UPPER = 100
CCI_LOWER = -100


# -----------------------------------------------------------------------------
# ASSETS
# -----------------------------------------------------------------------------

# Your Firefly AI preferred OTC pairs.
#
# IQ Option may not expose every pair at every moment. The scanner therefore
# skips unavailable assets rather than terminating the entire program.

ASSETS = [
    "USDJPY-OTC",
    "AUDUSD-OTC",
    "EURJPY-OTC",
    "AUDJPY-OTC",
    "GBPJPY-OTC",
    "GBPAUD-OTC",
    "GBPCAD-OTC",
    "CADJPY-OTC",
]


# -----------------------------------------------------------------------------
# CONNECTION SETTINGS
# -----------------------------------------------------------------------------

MAX_CONNECT_RETRIES = 5
RECONNECT_DELAY = 10

# If an asset repeatedly fails, don't hammer the API.
ASSET_ERROR_COOLDOWN = 30


# -----------------------------------------------------------------------------
# TIMEZONE
# -----------------------------------------------------------------------------

# Nigeria / West Africa time.
# We use a fixed UTC+1 timezone because ZoneInfo can be problematic on some
# Windows installations without timezone data.

WAT = timezone(timedelta(hours=1))


# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout)
    ],
)

logger = logging.getLogger("FIREFLY-KELTNER-CCI")


# =============================================================================
# VALIDATION
# =============================================================================

def validate_configuration():
    """
    Validate required environment variables before starting.
    """

    missing = []

    if not IQ_EMAIL:
        missing.append("IQ_EMAIL")

    if not IQ_PASSWORD:
        missing.append("IQ_PASSWORD")

    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")

    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")

    if missing:
        logger.error(
            "Missing required environment variables: %s",
            ", ".join(missing)
        )
        sys.exit(1)


# =============================================================================
# TELEGRAM
# =============================================================================

def send_telegram_message(message):
    """
    Send a message to the configured Telegram chat/channel.

    Returns:
        True  -> successfully sent
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
        "disable_web_page_preview": True,
    }

    try:

        response = requests.post(
            url,
            json=payload,
            timeout=15,
        )

        if response.status_code == 200:
            logger.info("Telegram notification sent.")
            return True

        logger.error(
            "Telegram API error: HTTP %s - %s",
            response.status_code,
            response.text,
        )

        return False

    except requests.exceptions.Timeout:
        logger.error("Telegram request timed out.")

    except requests.exceptions.ConnectionError:
        logger.error("Telegram connection error.")

    except requests.exceptions.RequestException as exc:
        logger.error("Telegram request failed: %s", exc)

    except Exception as exc:
        logger.error(
            "Unexpected Telegram error: %s",
            exc,
        )

    return False


# =============================================================================
# IQ OPTION CONNECTION
# =============================================================================

class IQOptionConnection:
    """
    Handles IQ Option connection and reconnection.
    """

    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.api = None
        self.lock = threading.Lock()

    # -------------------------------------------------------------------------
    # CONNECT
    # -------------------------------------------------------------------------

    def connect(self):

        with self.lock:

            for attempt in range(1, MAX_CONNECT_RETRIES + 1):

                try:

                    logger.info(
                        "Connecting to IQ Option "
                        "(attempt %s/%s)...",
                        attempt,
                        MAX_CONNECT_RETRIES,
                    )

                    self.api = IQ_Option(
                        self.email,
                        self.password,
                    )

                    check, reason = self.api.connect()

                    if check:

                        logger.info(
                            "Connected to IQ Option successfully."
                        )

                        try:
                            self.api.change_balance(ACCOUNT_TYPE)

                            logger.info(
                                "Account selected: %s",
                                ACCOUNT_TYPE,
                            )

                        except Exception as exc:
                            logger.warning(
                                "Could not change account type: %s",
                                exc,
                            )

                        return True

                    logger.error(
                        "IQ Option connection failed: %s",
                        reason,
                    )

                except Exception as exc:

                    logger.error(
                        "Connection exception: %s",
                        exc,
                    )

                if attempt < MAX_CONNECT_RETRIES:

                    logger.info(
                        "Retrying connection in %s seconds...",
                        RECONNECT_DELAY,
                    )

                    time.sleep(RECONNECT_DELAY)

        return False

    # -------------------------------------------------------------------------
    # ENSURE CONNECTION
    # -------------------------------------------------------------------------

    def ensure_connected(self):

        try:

            if self.api is None:
                return self.connect()

            # IQ Option's check_connect() tells us whether the websocket
            # connection is still alive.

            if self.api.check_connect():
                return True

            logger.warning(
                "IQ Option connection lost. Reconnecting..."
            )

        except Exception as exc:

            logger.warning(
                "Connection check failed: %s",
                exc,
            )

        return self.connect()


# =============================================================================
# TECHNICAL INDICATORS
# =============================================================================

def calculate_true_range(df):
    """
    Calculate True Range.
    """

    previous_close = df["close"].shift(1)

    range_1 = df["high"] - df["low"]

    range_2 = (df["high"] - previous_close).abs()

    range_3 = (df["low"] - previous_close).abs()

    true_range = pd.concat(
        [
            range_1,
            range_2,
            range_3,
        ],
        axis=1,
    ).max(axis=1)

    return true_range


# =============================================================================
# ATR
# =============================================================================

def calculate_atr(df, period=14):
    """
    Calculate Average True Range.

    Wilder-style ATR is approximated using an EMA, which is a common
    implementation for Keltner Channels.
    """

    true_range = calculate_true_range(df)

    atr = true_range.ewm(
        alpha=1 / period,
        adjust=False,
        min_periods=period,
    ).mean()

    return atr


# =============================================================================
# KELTNER CHANNEL
# =============================================================================

def calculate_keltner_channels(
    df,
    ema_period=25,
    atr_period=15,
    multiplier=1.0,
):
    """
    Calculate Keltner Channels.

    Middle:
        EMA(close, 25)

    Upper:
        EMA(close, 25) + ATR(15) * 1

    Lower:
        EMA(close, 25) - ATR(15) * 1
    """

    middle = df["close"].ewm(
        span=ema_period,
        adjust=False,
        min_periods=ema_period,
    ).mean()

    atr = calculate_atr(
        df,
        period=atr_period,
    )

    upper = middle + (
        atr * multiplier
    )

    lower = middle - (
        atr * multiplier
    )

    return middle, upper, lower


# =============================================================================
# CCI
# =============================================================================

def calculate_cci(df, period=15):
    """
    Calculate Commodity Channel Index.

    Typical Price:
        (High + Low + Close) / 3

    Mean Deviation:
        mean(abs(TP - SMA(TP)))

    CCI:
        (TP - SMA(TP)) / (0.015 * Mean Deviation)
    """

    typical_price = (
        df["high"]
        + df["low"]
        + df["close"]
    ) / 3.0

    sma = typical_price.rolling(
        window=period,
        min_periods=period,
    ).mean()

    mean_deviation = typical_price.rolling(
        window=period,
        min_periods=period,
    ).apply(
        lambda values: np.mean(
            np.abs(values - np.mean(values))
        ),
        raw=True,
    )

    denominator = 0.015 * mean_deviation

    cci = (
        (typical_price - sma)
        / denominator.replace(0, np.nan)
    )

    return cci


# =============================================================================
# DATAFRAME PREPARATION
# =============================================================================

def prepare_dataframe(candles):
    """
    Convert IQ Option candle data into a pandas DataFrame.
    """

    if not candles:
        return None

    df = pd.DataFrame(candles)

    required_columns = [
        "from",
        "open",
        "close",
        "min",
        "max",
    ]

    for column in required_columns:

        if column not in df.columns:

            logger.error(
                "Missing candle field: %s",
                column,
            )

            return None

    # IQ Option uses:
    #
    #   max -> high
    #   min -> low

    df = df.rename(
        columns={
            "max": "high",
            "min": "low",
        }
    )

    df["timestamp"] = pd.to_datetime(
        df["from"],
        unit="s",
        utc=True,
    )

    numeric_columns = [
        "open",
        "high",
        "low",
        "close",
    ]

    for column in numeric_columns:

        df[column] = pd.to_numeric(
            df[column],
            errors="coerce",
        )

    df = df.sort_values(
        "timestamp"
    ).drop_duplicates(
        subset=["timestamp"]
    )

    df = df.reset_index(drop=True)

    return df


# =============================================================================
# INDICATOR ENGINE
# =============================================================================

def calculate_indicators(df):
    """
    Add all strategy indicators to the DataFrame.
    """

    df = df.copy()

    (
        df["keltner_middle"],
        df["keltner_upper"],
        df["keltner_lower"],
    ) = calculate_keltner_channels(
        df,
        ema_period=KELTNER_EMA_PERIOD,
        atr_period=KELTNER_ATR_PERIOD,
        multiplier=KELTNER_MULTIPLIER,
    )

    df["cci"] = calculate_cci(
        df,
        period=CCI_PERIOD,
    )

    return df


# =============================================================================
# SIGNAL DETECTION
# =============================================================================

def detect_signal(df):
    """
    Detect a confirmed Keltner + CCI signal.

    The final two completed candles are evaluated.

    BUY:

        Candle 1:
            close > upper KC
            CCI > +100

        Candle 2:
            close > upper KC
            CCI > +100

    SELL:

        Candle 1:
            close < lower KC
            CCI < -100

        Candle 2:
            close < lower KC
            CCI < -100

    Returns:
        dict containing signal information
        OR
        None
    """

    if df is None or len(df) < 3:
        return None

    # -------------------------------------------------------------------------
    # Use the final two completed candles.
    # -------------------------------------------------------------------------

    first = df.iloc[-2]
    second = df.iloc[-1]

    # Ensure indicators are available.

    required_values = [
        first["keltner_upper"],
        first["keltner_lower"],
        first["cci"],
        second["keltner_upper"],
        second["keltner_lower"],
        second["cci"],
    ]

    if any(
        pd.isna(value)
        for value in required_values
    ):
        return None

    # -------------------------------------------------------------------------
    # BUY CONDITIONS
    # -------------------------------------------------------------------------

    first_buy_breakout = (
        first["close"]
        > first["keltner_upper"]
    )

    first_buy_cci = (
        first["cci"]
        > CCI_UPPER
    )

    second_buy_breakout = (
        second["close"]
        > second["keltner_upper"]
    )

    second_buy_cci = (
        second["cci"]
        > CCI_UPPER
    )

    buy_confirmed = (
        first_buy_breakout
        and first_buy_cci
        and second_buy_breakout
        and second_buy_cci
    )

    if buy_confirmed:

        return {
            "direction": "BUY",
            "signal": "CALL",
            "candle_time": second["timestamp"],
            "close": float(second["close"]),
            "cci": float(second["cci"]),
            "keltner_upper": float(
                second["keltner_upper"]
            ),
            "keltner_middle": float(
                second["keltner_middle"]
            ),
            "keltner_lower": float(
                second["keltner_lower"]
            ),
        }

    # -------------------------------------------------------------------------
    # SELL CONDITIONS
    # -------------------------------------------------------------------------

    first_sell_breakout = (
        first["close"]
        < first["keltner_lower"]
    )

    first_sell_cci = (
        first["cci"]
        < CCI_LOWER
    )

    second_sell_breakout = (
        second["close"]
        < second["keltner_lower"]
    )

    second_sell_cci = (
        second["cci"]
        < CCI_LOWER
    )

    sell_confirmed = (
        first_sell_breakout
        and first_sell_cci
        and second_sell_breakout
        and second_sell_cci
    )

    if sell_confirmed:

        return {
            "direction": "SELL",
            "signal": "PUT",
            "candle_time": second["timestamp"],
            "close": float(second["close"]),
            "cci": float(second["cci"]),
            "keltner_upper": float(
                second["keltner_upper"]
            ),
            "keltner_middle": float(
                second["keltner_middle"]
            ),
            "keltner_lower": float(
                second["keltner_lower"]
            ),
        }

    return None


# =============================================================================
# SIGNAL FORMAT
# =============================================================================


# =============================================================================
# CURRENCY / COUNTRY FLAGS
# =============================================================================

CURRENCY_FLAGS = {
    "USD": "🇺🇸",   # United States Dollar
    "JPY": "🇯🇵",   # Japanese Yen
    "AUD": "🇦🇺",   # Australian Dollar
    "EUR": "🇪🇺",   # Euro / European Union
    "GBP": "🇬🇧",   # British Pound
    "CAD": "🇨🇦",   # Canadian Dollar
}


# =============================================================================
# EXTRACT CURRENCIES FROM SYMBOL
# =============================================================================

def get_currency_pair_info(symbol):
    """
    Extract the two currencies from an IQ Option symbol.

    Examples:

        USDJPY-OTC
        -> 🇺🇸 USD / 🇯🇵 JPY

        AUDUSD-OTC
        -> 🇦🇺 AUD / 🇺🇸 USD

        EURJPY-OTC
        -> 🇪🇺 EUR / 🇯🇵 JPY

        GBPJPY-OTC
        -> 🇬🇧 GBP / 🇯🇵 JPY
    """

    # Remove OTC suffix.

    clean_symbol = symbol.upper().replace("-OTC", "")

    # The supported FX pairs consist of six characters:
    #
    # USDJPY
    # EURJPY
    # AUDUSD
    # etc.

    if len(clean_symbol) != 6:

        return (
            "💱",
            "💱",
            clean_symbol,
            ""
        )

    base_currency = clean_symbol[:3]
    quote_currency = clean_symbol[3:]

    base_flag = CURRENCY_FLAGS.get(
        base_currency,
        "💱"
    )

    quote_flag = CURRENCY_FLAGS.get(
        quote_currency,
        "💱"
    )

    return (
        base_flag,
        quote_flag,
        base_currency,
        quote_currency,
    )


# =============================================================================
# SIGNAL FORMAT
# =============================================================================

def format_signal_message(
    symbol,
    signal_data,
):
    """
    Format the Telegram strategy alert.

    The currency-pair flags are generated automatically from the symbol.
    """

    direction = signal_data["direction"]

    if direction == "BUY":

        signal_icon = "🟢"
        signal_text = "BUY (Call)"

    else:

        signal_icon = "🔴"
        signal_text = "SELL (Put)"

    # -------------------------------------------------------------------------
    # Currency pair information
    # -------------------------------------------------------------------------

    (
        base_flag,
        quote_flag,
        base_currency,
        quote_currency,
    ) = get_currency_pair_info(symbol)

    # Remove OTC from the displayed pair.

    display_symbol = (
        symbol.upper()
        .replace("-OTC", "")
    )

    # Format as:

    # 🇺🇸 USD / 🇯🇵 JPY

    currency_pair_display = (
        f"{base_flag} {base_currency} / "
        f"{quote_flag} {quote_currency}"
    )

    # -------------------------------------------------------------------------
    # Timestamp
    # -------------------------------------------------------------------------

    candle_time = signal_data["candle_time"]

    if hasattr(candle_time, "to_pydatetime"):

        candle_datetime = (
            candle_time.to_pydatetime()
        )

    else:

        candle_datetime = candle_time

    candle_datetime = candle_datetime.astimezone(
        timezone.utc
    )

    timestamp = candle_datetime.strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    # -------------------------------------------------------------------------
    # Indicator values
    # -------------------------------------------------------------------------

    close_price = signal_data["close"]
    cci_value = signal_data["cci"]

    upper = signal_data["keltner_upper"]
    middle = signal_data["keltner_middle"]
    lower = signal_data["keltner_lower"]

    # -------------------------------------------------------------------------
    # Telegram message
    # -------------------------------------------------------------------------

    message = (
        "🚨 <b>FIREFLY AI STRATEGY SIGNAL ALERT</b>\n"
        "\n"
        f"<b>Currencies:</b> {currency_pair_display}\n"
        f"<b>Signal Type:</b> "
        f"{signal_icon} <b>{signal_text}</b>\n"
        "\n"
        f"<b>{display_symbol}</b>\n"
        f"<b>Pair:</b> {base_flag} {quote_flag} "
        f"<b>Timeframe:</b> ⏳ 2 Minutes (M2)\n"
        f"<b>Close Price:</b> {close_price:.6f}\n"
        f"<b>CCI (15):</b> {cci_value:.2f}\n"
        "\n"
        f"<b>Keltner Upper:</b> {upper:.6f}\n"
        f"<b>Keltner Middle:</b> {middle:.6f}\n"
        f"<b>Keltner Lower:</b> {lower:.6f}\n"
        "\n"
        f"<b>Timestamp:</b> {timestamp}\n"
        "\n"
        "📊 <b>Strategy:</b> Keltner Channel + CCI\n"
        "📈 <b>KC:</b> EMA 25 / ATR 15 / Multiplier 1\n"
        "📐 <b>CCI:</b> Period 15\n"
        "\n"
        "⚠️ <i>Signal only — no automatic trade executed.</i>"
    )

    return message



# =============================================================================
# ASSET STATE
# =============================================================================

class AssetState:
    """
    Keeps track of the latest processed candle and breakout state for one asset.

    This prevents duplicate Telegram alerts.
    """

    def __init__(self):

        self.last_processed_candle = None

        # Last signal candle that generated a Telegram alert.
        self.last_signal_candle = None

        # Current breakout cycle:
        #
        # None -> no active breakout
        # BUY  -> bullish breakout cycle
        # SELL -> bearish breakout cycle

        self.active_breakout = None

        # Timestamp of the first candle in the active breakout.

        self.breakout_start = None

        # Last time an asset generated an API error.

        self.last_error_time = 0


# =============================================================================
# MARKET SCANNER
# =============================================================================

class MarketScanner:

    def __init__(self, connection):

        self.connection = connection

        self.asset_states = {
            asset: AssetState()
            for asset in ASSETS
        }

        self.running = True

    # -------------------------------------------------------------------------
    # GET CANDLES
    # -------------------------------------------------------------------------

    def get_candles(self, symbol):

        if not self.connection.ensure_connected():

            logger.error(
                "Unable to establish IQ Option connection."
            )

            return None

        try:

            candles = self.connection.api.get_candles(
                symbol,
                TIMEFRAME_SECONDS,
                CANDLE_COUNT,
                time.time(),
            )

            if not candles:

                logger.warning(
                    "%s -> No candles returned.",
                    symbol,
                )

                return None

            return candles

        except Exception as exc:

            logger.error(
                "%s -> Candle retrieval failed: %s",
                symbol,
                exc,
            )

            return None

    # -------------------------------------------------------------------------
    # FILTER COMPLETED CANDLES
    # -------------------------------------------------------------------------

    def get_completed_candles(self, candles):

        if not candles:
            return []

        current_time = time.time()

        completed = []

        for candle in candles:

            try:

                candle_start = float(
                    candle["from"]
                )

                candle_end = (
                    candle_start
                    + TIMEFRAME_SECONDS
                )

                # Only process candles that have completely closed.

                if candle_end <= current_time:

                    completed.append(candle)

            except (
                KeyError,
                TypeError,
                ValueError,
            ):

                continue

        return completed

    # -------------------------------------------------------------------------
    # BREAKOUT CYCLE MANAGEMENT
    # -------------------------------------------------------------------------

    def manage_breakout_state(
        self,
        symbol,
        signal,
        candle_time,
    ):
        """
        Prevent repeated signals during the same breakout.

        Example:

            Candle 1 -> breakout
            Candle 2 -> confirmation -> SIGNAL

            Candle 3 -> still above KC
            Candle 4 -> still above KC

        Candle 3 and Candle 4 will NOT repeatedly generate the same alert.

        The breakout cycle resets when price leaves the breakout zone.
        """

        state = self.asset_states[symbol]

        if signal is None:
            return False

        direction = signal["direction"]

        # If this is a new direction, allow the signal.

        if state.active_breakout != direction:

            state.active_breakout = direction
            state.breakout_start = candle_time

            return True

        # Same breakout direction is already active.

        return False

    # -------------------------------------------------------------------------
    # RESET BREAKOUT
    # -------------------------------------------------------------------------

    def reset_breakout_if_needed(self, symbol, df):

        state = self.asset_states[symbol]

        if state.active_breakout is None:
            return

        if df is None or len(df) == 0:
            return

        latest = df.iloc[-1]

        close = latest["close"]
        upper = latest["keltner_upper"]
        lower = latest["keltner_lower"]

        if pd.isna(upper) or pd.isna(lower):
            return

        # ---------------------------------------------------------------------
        # BUY breakout ends when price closes back inside/below Upper KC.
        # ---------------------------------------------------------------------

        if state.active_breakout == "BUY":

            if close <= upper:

                logger.info(
                    "%s -> BUY breakout cycle ended.",
                    symbol,
                )

                state.active_breakout = None
                state.breakout_start = None

        # ---------------------------------------------------------------------
        # SELL breakout ends when price closes back inside/above Lower KC.
        # ---------------------------------------------------------------------

        elif state.active_breakout == "SELL":

            if close >= lower:

                logger.info(
                    "%s -> SELL breakout cycle ended.",
                    symbol,
                )

                state.active_breakout = None
                state.breakout_start = None

    # -------------------------------------------------------------------------
    # PROCESS ASSET
    # -------------------------------------------------------------------------

    def process_asset(self, symbol):

        state = self.asset_states[symbol]

        # Prevent excessive requests after an API error.

        if (
            time.time() - state.last_error_time
            < ASSET_ERROR_COOLDOWN
        ):
            return

        try:

            candles = self.get_candles(symbol)

            if not candles:
                return

            completed_candles = (
                self.get_completed_candles(
                    candles
                )
            )

            if len(completed_candles) < 30:

                logger.warning(
                    "%s -> Not enough completed candles.",
                    symbol,
                )

                return

            df = prepare_dataframe(
                completed_candles
            )

            if df is None:
                return

            df = calculate_indicators(df)

            if len(df) < 30:
                return

            # -----------------------------------------------------------------
            # Identify newest completed candle.
            # -----------------------------------------------------------------

            latest_candle_time = df.iloc[-1][
                "timestamp"
            ]

            # We only process a candle once.

            if (
                state.last_processed_candle
                is not None
                and latest_candle_time
                <= state.last_processed_candle
            ):

                return

            state.last_processed_candle = (
                latest_candle_time
            )

            # -----------------------------------------------------------------
            # First reset an old breakout if price has returned through KC.
            # -----------------------------------------------------------------

            self.reset_breakout_if_needed(
                symbol,
                df,
            )

            # -----------------------------------------------------------------
            # Detect new confirmed signal.
            # -----------------------------------------------------------------

            signal = detect_signal(df)

            if signal is None:

                logger.debug(
                    "%s -> No confirmed signal.",
                    symbol,
                )

                return

            # -----------------------------------------------------------------
            # Check breakout cycle.
            # -----------------------------------------------------------------

            should_alert = (
                self.manage_breakout_state(
                    symbol,
                    signal,
                    latest_candle_time,
                )
            )

            if not should_alert:

                logger.info(
                    "%s -> %s confirmation already alerted "
                    "for current breakout.",
                    symbol,
                    signal["direction"],
                )

                return

            # -----------------------------------------------------------------
            # Additional duplicate protection.
            # -----------------------------------------------------------------

            if (
                state.last_signal_candle
                == latest_candle_time
            ):

                logger.info(
                    "%s -> Duplicate signal suppressed.",
                    symbol,
                )

                return

            state.last_signal_candle = (
                latest_candle_time
            )

            # -----------------------------------------------------------------
            # Log signal.
            # -----------------------------------------------------------------

            logger.info(
                "%s -> %s SIGNAL | Close=%.6f | CCI=%.2f",
                symbol,
                signal["direction"],
                signal["close"],
                signal["cci"],
            )

            # -----------------------------------------------------------------
            # Telegram.
            # -----------------------------------------------------------------

            message = format_signal_message(
                symbol,
                signal,
            )

            send_telegram_message(
                message
            )

        except Exception as exc:

            state.last_error_time = time.time()

            logger.error(
                "%s -> Processing error: %s",
                symbol,
                exc,
            )

            logger.debug(
                traceback.format_exc()
            )

    # -------------------------------------------------------------------------
    # RUN ONE SCAN
    # -------------------------------------------------------------------------

    def scan_all_assets(self):

        for symbol in ASSETS:

            if not self.running:
                break

            try:

                self.process_asset(
                    symbol
                )

            except Exception as exc:

                logger.error(
                    "%s -> Unexpected scanner error: %s",
                    symbol,
                    exc,
                )

    # -------------------------------------------------------------------------
    # START
    # -------------------------------------------------------------------------

    def start(self):

        logger.info("=" * 80)
        logger.info(
            "FIREFLY AI - KELTNER + CCI SCANNER"
        )
        logger.info("=" * 80)

        logger.info(
            "Timeframe      : M2 / 2 minutes"
        )

        logger.info(
            "Keltner EMA    : %s",
            KELTNER_EMA_PERIOD,
        )

        logger.info(
            "Keltner ATR    : %s",
            KELTNER_ATR_PERIOD,
        )

        logger.info(
            "Keltner Mult   : %.2f",
            KELTNER_MULTIPLIER,
        )

        logger.info(
            "CCI Period     : %s",
            CCI_PERIOD,
        )

        logger.info(
            "CCI Upper      : +%s",
            CCI_UPPER,
        )

        logger.info(
            "CCI Lower      : %s",
            CCI_LOWER,
        )

        logger.info(
            "Account        : %s",
            ACCOUNT_TYPE,
        )

        logger.info(
            "Assets         : %s",
            len(ASSETS),
        )

        logger.info(
            "Polling        : %s seconds",
            POLL_INTERVAL,
        )

        logger.info("=" * 80)

        while self.running:

            try:

                # -------------------------------------------------------------
                # Ensure connection.
                # -------------------------------------------------------------

                if not self.connection.ensure_connected():

                    logger.error(
                        "IQ Option unavailable. "
                        "Waiting before retry..."
                    )

                    time.sleep(
                        RECONNECT_DELAY
                    )

                    continue

                # -------------------------------------------------------------
                # Scan all configured assets.
                # -------------------------------------------------------------

                self.scan_all_assets()

                # -------------------------------------------------------------
                # Wait before next scan.
                # -------------------------------------------------------------

                time.sleep(
                    POLL_INTERVAL
                )

            except KeyboardInterrupt:

                logger.info(
                    "Keyboard interrupt received."
                )

                self.stop()

            except Exception as exc:

                logger.error(
                    "Main scanner loop error: %s",
                    exc,
                )

                logger.debug(
                    traceback.format_exc()
                )

                time.sleep(
                    RECONNECT_DELAY
                )

    # -------------------------------------------------------------------------
    # STOP
    # -------------------------------------------------------------------------

    def stop(self):

        self.running = False

        logger.info(
            "Firefly AI scanner stopped."
        )


# =============================================================================
# TELEGRAM TEST
# =============================================================================

def test_telegram():

    logger.info(
        "Testing Telegram connection..."
    )

    test_message = (
        "🟢 <b>FIREFLY AI</b>\n\n"
        "Keltner + CCI scanner connected successfully.\n"
        "📊 Timeframe: M2\n"
        "📈 Strategy: Keltner Channel + CCI\n"
        "⚠️ Signal-only mode."
    )

    return send_telegram_message(
        test_message
    )


# =============================================================================
# MAIN
# =============================================================================

def main():

    print()
    print("=" * 80)
    print("FIREFLY AI - KELTNER CHANNEL + CCI SCANNER")
    print("=" * 80)
    print()

    # -------------------------------------------------------------------------
    # Validate configuration.
    # -------------------------------------------------------------------------

    validate_configuration()

    # -------------------------------------------------------------------------
    # Telegram test.
    # -------------------------------------------------------------------------

    logger.info(
        "Testing Telegram Bot API..."
    )

    telegram_ok = test_telegram()

    if not telegram_ok:

        logger.warning(
            "Telegram test failed."
        )

        logger.warning(
            "The scanner will continue, but "
            "Telegram notifications may not work."
        )

    # -------------------------------------------------------------------------
    # Connect IQ Option.
    # -------------------------------------------------------------------------

    connection = IQOptionConnection(
        IQ_EMAIL,
        IQ_PASSWORD,
    )

    if not connection.connect():

        logger.error(
            "Unable to connect to IQ Option."
        )

        sys.exit(1)

    # -------------------------------------------------------------------------
    # Create scanner.
    # -------------------------------------------------------------------------

    scanner = MarketScanner(
        connection
    )

    # -------------------------------------------------------------------------
    # Start scanner.
    # -------------------------------------------------------------------------

    try:

        scanner.start()

    except KeyboardInterrupt:

        scanner.stop()

    except Exception as exc:

        logger.critical(
            "Fatal application error: %s",
            exc,
        )

        logger.debug(
            traceback.format_exc()
        )

    finally:

        scanner.stop()

        logger.info(
            "FIREFLY AI shutdown complete."
        )


# =============================================================================
# ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    main()

