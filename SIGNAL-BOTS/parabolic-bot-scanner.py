import os
import sys
import time
import logging
import statistics
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option
import iqoptionapi.stable_api as stable_api


# ============================================================
# FIREFLY AI - EMA 6/24 + PARABOLIC SAR REAL-TIME SCANNER
# SIGNAL ONLY - NO TRADES ARE PLACED
# ============================================================

load_dotenv()

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Market
TIMEFRAME = 60
CANDLE_COUNT = 80
POLL_INTERVAL = 5
RECONNECT_DELAY = 5
ASSET_REFRESH_TIMEOUT = 15

# EMA
EMA_FAST_PERIOD = 6
EMA_SLOW_PERIOD = 24

# Parabolic SAR
PSAR_STEP = 0.05
PSAR_MAX = 0.20

# Signal confirmation
MIN_SAR_DOTS = 3
MAX_SAR_DOTS = 7
MIN_CROSS_AGE = 1
MAX_CROSS_AGE = 3

# Volatility / spike filters
ATR_PERIOD = 14
ATR_BASELINE_PERIOD = 30
ATR_SPIKE_MULTIPLIER = 2.0
BODY_ATR_MULTIPLIER = 1.5

# Eight configured OTC pairs
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

for name, value in {
    "IQ_EMAIL": IQ_EMAIL,
    "IQ_PASSWORD": IQ_PASSWORD,
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
}.items():
    if not value:
        print(f"ERROR: {name} is missing from .env")
        sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("FIREFLY-EMA-SAR")

API = None
LAST_PROCESSED_CANDLE = {}
LAST_SIGNAL_CYCLE = {}
CURRENT_CYCLE = {}
ASSET_REFRESH_LOCK = False
WAT = timezone(timedelta(hours=1))


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(url, json=payload, timeout=10)

        if response.ok:
            logger.info("Telegram notification sent.")
            return True

        logger.error("Telegram API error: %s", response.text)

    except requests.RequestException as exc:
        logger.error("Telegram request failed: %s", exc)

    return False


# ============================================================
# TIME
# ============================================================

def format_wat_time(timestamp=None):
    if timestamp is None:
        dt = datetime.now(WAT)
    else:
        dt = datetime.fromtimestamp(
            float(timestamp), tz=timezone.utc
        ).astimezone(WAT)

    return dt.strftime("%Y-%m-%d %H:%M:%S WAT")


def get_server_time():
    try:
        server_timestamp = API.api.timesync.server_timestamp
        if server_timestamp:
            return float(server_timestamp)
    except Exception:
        pass

    return time.time()


# ============================================================
# IQ OPTION CONNECTION
# ============================================================

def connect_iq_option():
    global API

    while True:
        try:
            logger.info("=" * 70)
            logger.info("CONNECTING TO IQ OPTION")
            logger.info("=" * 70)

            API = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
            check, reason = API.connect()

            if check:
                try:
                    API.change_balance("PRACTICE")
                    logger.info(
                        "Connected successfully. Practice balance: $%.2f",
                        API.get_balance(),
                    )
                except Exception as exc:
                    logger.warning(
                        "Balance unavailable after connection: %s", exc
                    )

                return True

            logger.error("IQ Option connection failed: %s", reason)

        except Exception as exc:
            logger.exception("Connection exception: %s", exc)

        time.sleep(RECONNECT_DELAY)


def connection_alive():
    try:
        return API is not None and API.check_connect()
    except Exception:
        return False


# ============================================================
# DYNAMIC OTC ASSET MAPPING
# ============================================================

def refresh_otc_asset_mappings():
    global ASSET_REFRESH_LOCK

    if API is None or ASSET_REFRESH_LOCK:
        return False

    ASSET_REFRESH_LOCK = True

    try:
        missing = [
            pair for pair in OTC_PAIRS
            if stable_api.OP_code.ACTIVES.get(pair) is None
        ]

        if not missing:
            return True

        logger.info(
            "Refreshing OTC mappings for: %s",
            ", ".join(missing),
        )

        API.api.api_option_init_all_result = None
        start = time.time()
        API.api.get_api_option_init_all()

        while (
            API.api.api_option_init_all_result is None
            and time.time() - start < ASSET_REFRESH_TIMEOUT
        ):
            time.sleep(0.1)

        result = API.api.api_option_init_all_result

        if result is None or not result.get("isSuccessful"):
            logger.error("OTC asset initialization failed or timed out.")
            return False

        result_data = result.get("result", {})

        for market_type in ("binary", "turbo"):
            actives = result_data.get(
                market_type, {}
            ).get("actives", {})

            for active_id, active_data in actives.items():
                try:
                    raw_name = active_data.get("name", "")
                    asset_name = (
                        raw_name.split(".", 1)[1]
                        if "." in raw_name
                        else raw_name
                    )

                    if asset_name in OTC_PAIRS:
                        stable_api.OP_code.ACTIVES[
                            asset_name
                        ] = int(active_id)

                except Exception:
                    continue

        available = 0

        for pair in OTC_PAIRS:
            active_id = stable_api.OP_code.ACTIVES.get(pair)

            if active_id is not None:
                logger.info(
                    "READY:   %-15s -> %s", pair, active_id
                )
                available += 1
            else:
                logger.warning("MISSING: %-15s", pair)

        logger.info(
            "%d/%d configured OTC pairs available.",
            available,
            len(OTC_PAIRS),
        )

        # Do not stop the whole scanner if IQ Option does not
        # expose one of the configured OTC symbols.
        return available > 0

    except Exception as exc:
        logger.exception("OTC mapping refresh failed: %s", exc)
        return False

    finally:
        ASSET_REFRESH_LOCK = False


# ============================================================
# COMPLETED 1-MINUTE CANDLES
# ============================================================

def get_completed_candles(pair):
    try:
        if stable_api.OP_code.ACTIVES.get(pair) is None:
            logger.warning("%s is not mapped. Skipping.", pair)
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
            candle_from = float(candle.get("from", 0))
            candle_to = float(
                candle.get("to", candle_from + TIMEFRAME)
            )

            if candle_to <= end_time:
                completed.append(candle)

        completed.sort(
            key=lambda candle: float(candle.get("from", 0))
        )

        return completed[-CANDLE_COUNT:]

    except Exception as exc:
        logger.error("%s candle error: %s", pair, exc)
        return []


def extract_ohlc(candles):
    opens = [float(c["open"]) for c in candles]
    highs = [float(c["max"]) for c in candles]
    lows = [float(c["min"]) for c in candles]
    closes = [float(c["close"]) for c in candles]
    return opens, highs, lows, closes


# ============================================================
# EMA
# ============================================================

def calculate_ema(values, period):
    if len(values) < period:
        return []

    multiplier = 2.0 / (period + 1.0)
    ema_values = [sum(values[:period]) / period]

    for price in values[period:]:
        previous = ema_values[-1]
        ema_values.append(
            (price - previous) * multiplier + previous
        )

    return [None] * (period - 1) + ema_values


# ============================================================
# ATR
# ============================================================

def calculate_true_ranges(highs, lows, closes):
    result = []

    for i in range(len(highs)):
        if i == 0:
            tr = highs[i] - lows[i]
        else:
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )

        result.append(tr)

    return result


def calculate_atr(highs, lows, closes, period=14):
    tr_values = calculate_true_ranges(highs, lows, closes)

    if len(tr_values) < period:
        return []

    atr_values = [sum(tr_values[:period]) / period]

    for tr in tr_values[period:]:
        previous = atr_values[-1]
        atr_values.append(
            ((previous * (period - 1)) + tr) / period
        )

    return [None] * (period - 1) + atr_values


# ============================================================
# PARABOLIC SAR
# ============================================================

def calculate_parabolic_sar(
    highs,
    lows,
    step=0.05,
    maximum=0.20,
):
    if len(highs) < 2:
        return []

    sar = [None] * len(highs)

    bullish = highs[1] >= highs[0]

    if bullish:
        sar[0] = lows[0]
        extreme_point = max(highs[0], highs[1])
    else:
        sar[0] = highs[0]
        extreme_point = min(lows[0], lows[1])

    acceleration = step

    for i in range(1, len(highs)):
        previous_sar = sar[i - 1]

        current_sar = (
            previous_sar
            + acceleration * (extreme_point - previous_sar)
        )

        if bullish:
            current_sar = min(
                current_sar,
                lows[i - 1],
            )

            if i >= 2:
                current_sar = min(
                    current_sar,
                    lows[i - 2],
                )

            if lows[i] < current_sar:
                bullish = False
                current_sar = extreme_point
                extreme_point = lows[i]
                acceleration = step
            elif highs[i] > extreme_point:
                extreme_point = highs[i]
                acceleration = min(
                    acceleration + step,
                    maximum,
                )

        else:
            current_sar = max(
                current_sar,
                highs[i - 1],
            )

            if i >= 2:
                current_sar = max(
                    current_sar,
                    highs[i - 2],
                )

            if highs[i] > current_sar:
                bullish = True
                current_sar = extreme_point
                extreme_point = highs[i]
                acceleration = step
            elif lows[i] < extreme_point:
                extreme_point = lows[i]
                acceleration = min(
                    acceleration + step,
                    maximum,
                )

        sar[i] = current_sar

    return sar


def get_sar_direction(sar_value, high, low):
    if sar_value < low:
        return "UP"
    if sar_value > high:
        return "DOWN"
    return "NONE"


def count_current_sar_dots(sar_values, highs, lows):
    if not sar_values:
        return None, 0

    last = len(sar_values) - 1

    direction = get_sar_direction(
        sar_values[last],
        highs[last],
        lows[last],
    )

    if direction == "NONE":
        return None, 0

    count = 0

    for i in range(last, -1, -1):
        current_direction = get_sar_direction(
            sar_values[i],
            highs[i],
            lows[i],
        )

        if current_direction != direction:
            break

        count += 1

    return direction, count


# ============================================================
# EMA CROSSOVER
# ============================================================

def find_recent_crossover(
    fast_ema,
    slow_ema,
    min_age=1,
    max_age=3,
):
    n = len(fast_ema)

    if n < 3:
        return None

    newest = n - 1

    for age in range(min_age, max_age + 1):
        index = newest - age

        if index <= 0:
            continue

        fp = fast_ema[index - 1]
        sp = slow_ema[index - 1]
        fc = fast_ema[index]
        sc = slow_ema[index]

        if None in (fp, sp, fc, sc):
            continue

        if fp <= sp and fc > sc:
            return {
                "direction": "UP",
                "age": age,
                "index": index,
            }

        if fp >= sp and fc < sc:
            return {
                "direction": "DOWN",
                "age": age,
                "index": index,
            }

    return None


def find_latest_crossover(fast_ema, slow_ema):
    n = len(fast_ema)

    if n < 3:
        return None

    for index in range(n - 1, 0, -1):
        fp = fast_ema[index - 1]
        sp = slow_ema[index - 1]
        fc = fast_ema[index]
        sc = slow_ema[index]

        if None in (fp, sp, fc, sc):
            continue

        if fp <= sp and fc > sc:
            return {"direction": "UP", "index": index}

        if fp >= sp and fc < sc:
            return {"direction": "DOWN", "index": index}

    return None


# ============================================================
# VOLATILITY / SPIKE FILTER
# ============================================================

def volatility_check(
    opens,
    highs,
    lows,
    closes,
    atr_values,
):
    if not atr_values:
        return False, "ATR unavailable"

    current_atr = atr_values[-1]

    if current_atr is None or current_atr <= 0:
        return False, "Invalid ATR"

    baseline = [
        value for value in atr_values[:-1]
        if value is not None and value > 0
    ][-ATR_BASELINE_PERIOD:]

    if not baseline:
        return False, "ATR baseline unavailable"

    baseline_atr = statistics.median(baseline)

    if (
        ATR_SPIKE_MULTIPLIER is not None
        and current_atr > baseline_atr * ATR_SPIKE_MULTIPLIER
    ):
        return False, "ATR spike detected"

    body = abs(closes[-1] - opens[-1])

    if (
        BODY_ATR_MULTIPLIER is not None
        and body > current_atr * BODY_ATR_MULTIPLIER
    ):
        return False, "Large candle-body spike detected"

    return True, "OK"


# ============================================================
# SIGNAL STRENGTH
# ============================================================

def get_signal_strength(dot_count):
    if dot_count == 3:
        return "HIGH"
    if dot_count in (4, 5):
        return "MEDIUM"
    if dot_count in (6, 7):
        return "MODERATE"
    return "INVALID"


def dot_description(dot_count):
    if dot_count == 3:
        return "3 dots - Valid Range - OPTIMAL"
    return f"{dot_count} dots - Valid Range"


# ============================================================
# TREND-CYCLE COOLDOWN
# ============================================================

def update_trend_cycle(pair, latest_cross, candles):
    if latest_cross is None:
        return

    index = latest_cross["index"]
    direction = latest_cross["direction"]

    timestamp = int(float(candles[index]["from"]))

    cycle_key = (direction, timestamp)

    if CURRENT_CYCLE.get(pair) != cycle_key:
        CURRENT_CYCLE[pair] = cycle_key

        logger.info(
            "%-15s | New %s EMA cycle | %s",
            pair,
            direction,
            format_wat_time(timestamp),
        )


# ============================================================
# TELEGRAM SIGNAL FORMAT
# ============================================================

def build_signal_message(
    pair,
    signal_direction,
    dot_direction,
    dot_count,
    crossover,
    price,
    timestamp,
    atr_value,
    body_size,
):
    signal_type = (
        "BUY / LONG"
        if signal_direction == "BUY"
        else "SELL / SHORT"
    )

    if crossover["direction"] == "UP":
        crossover_text = (
            f"EMA 6 crossed above EMA 24 "
            f"{crossover['age']} candle"
            f"{'' if crossover['age'] == 1 else 's'} ago"
        )
    else:
        crossover_text = (
            f"EMA 6 crossed below EMA 24 "
            f"{crossover['age']} candle"
            f"{'' if crossover['age'] == 1 else 's'} ago"
        )

    sar_position = (
        "below candles"
        if dot_direction == "UP"
        else "above candles"
    )

    strength = get_signal_strength(dot_count)

    return (
        "🚨 <b>FIREFLY EMA/SAR SIGNAL</b>\n"
        "\n"
        f"📊 <b>Symbol:</b> {pair}\n"
        f"🎯 <b>Signal:</b> {signal_type}\n"
        f"📍 <b>Parabolic SAR:</b> {sar_position}\n"
        f"🔢 <b>SAR Dot Count:</b> "
        f"{dot_description(dot_count)}\n"
        f"🔀 <b>EMA Crossover:</b> {crossover_text}\n"
        f"⭐ <b>Signal Strength:</b> {strength}\n"
        "\n"
        f"💵 <b>Current Price:</b> {price:.6f}\n"
        f"📏 <b>ATR:</b> {atr_value:.6f}\n"
        f"📐 <b>Candle Body:</b> {body_size:.6f}\n"
        f"🕐 <b>Timestamp:</b> {format_wat_time(timestamp)}\n"
        "\n"
        "⏱ <b>Timeframe:</b> 1 Minute\n"
        "📈 <b>EMA:</b> 6 / 24\n"
        "⚙️ <b>PSAR:</b> Step 0.05 / Max 0.20\n"
    )


# ============================================================
# PROCESS ONE PAIR
# ============================================================

def process_pair(pair):
    try:
        candles = get_completed_candles(pair)

        minimum_required = max(
            EMA_SLOW_PERIOD + 10,
            ATR_PERIOD + ATR_BASELINE_PERIOD,
        )

        if len(candles) < minimum_required:
            return

        latest_timestamp = int(float(candles[-1]["from"]))

        # Process a closed candle only once.
        if LAST_PROCESSED_CANDLE.get(pair) == latest_timestamp:
            return

        LAST_PROCESSED_CANDLE[pair] = latest_timestamp

        opens, highs, lows, closes = extract_ohlc(candles)

        fast_ema = calculate_ema(
            closes, EMA_FAST_PERIOD
        )
        slow_ema = calculate_ema(
            closes, EMA_SLOW_PERIOD
        )
        sar_values = calculate_parabolic_sar(
            highs, lows, PSAR_STEP, PSAR_MAX
        )
        atr_values = calculate_atr(
            highs, lows, closes, ATR_PERIOD
        )

        if not fast_ema or not slow_ema:
            return
        if not sar_values or not atr_values:
            return

        # Maintain the current EMA trend cycle.
        latest_cross = find_latest_crossover(
            fast_ema, slow_ema
        )
        update_trend_cycle(
            pair, latest_cross, candles
        )

        # Required crossover must be 1-3 candles old.
        recent_cross = find_recent_crossover(
            fast_ema,
            slow_ema,
            MIN_CROSS_AGE,
            MAX_CROSS_AGE,
        )

        if recent_cross is None:
            logger.info(
                "%-15s | No EMA cross in last %d-%d candles.",
                pair,
                MIN_CROSS_AGE,
                MAX_CROSS_AGE,
            )
            return

        # Current SAR dot direction and count.
        dot_direction, dot_count = count_current_sar_dots(
            sar_values,
            highs,
            lows,
        )

        if dot_direction is None:
            logger.info(
                "%-15s | SAR position ambiguous.",
                pair,
            )
            return

        # Strict 3-7 filter.
        if not (
            MIN_SAR_DOTS <= dot_count <= MAX_SAR_DOTS
        ):
            logger.info(
                "%-15s | SAR dots=%d rejected; valid range is 3-7.",
                pair,
                dot_count,
            )
            return

        # EMA and SAR must agree.
        if (
            recent_cross["direction"] == "UP"
            and dot_direction == "UP"
        ):
            signal_direction = "BUY"

        elif (
            recent_cross["direction"] == "DOWN"
            and dot_direction == "DOWN"
        ):
            signal_direction = "SELL"

        else:
            logger.info(
                "%-15s | EMA/SAR direction mismatch.",
                pair,
            )
            return

        # Volatility/spike filter.
        allowed, reason = volatility_check(
            opens,
            highs,
            lows,
            closes,
            atr_values,
        )

        if not allowed:
            logger.info(
                "%-15s | Volatility filter: %s",
                pair,
                reason,
            )
            return

        # Same EMA trend cycle can only produce one alert.
        cycle = CURRENT_CYCLE.get(pair)

        if cycle is None:
            return

        if LAST_SIGNAL_CYCLE.get(pair) == cycle:
            logger.info(
                "%-15s | Duplicate blocked: same trend cycle.",
                pair,
            )
            return

        price = closes[-1]
        atr_value = atr_values[-1]
        body_size = abs(closes[-1] - opens[-1])

        message = build_signal_message(
            pair=pair,
            signal_direction=signal_direction,
            dot_direction=dot_direction,
            dot_count=dot_count,
            crossover=recent_cross,
            price=price,
            timestamp=latest_timestamp + TIMEFRAME,
            atr_value=atr_value,
            body_size=body_size,
        )

        logger.info(
            "%s SIGNAL | %s | SAR dots=%d | EMA cross age=%d",
            pair,
            signal_direction,
            dot_count,
            recent_cross["age"],
        )

        if send_telegram(message):
            LAST_SIGNAL_CYCLE[pair] = cycle

    except Exception as exc:
        logger.exception(
            "Error processing %s: %s", pair, exc
        )


# ============================================================
# SCAN ALL PAIRS
# ============================================================

def scan_all_pairs():
    # Sequential requests are intentional for the older
    # iqoptionapi websocket implementation.
    for pair in OTC_PAIRS:
        process_pair(pair)


# ============================================================
# MAIN LOOP
# ============================================================

def run_bot():
    logger.info("=" * 70)
    logger.info(
        "FIREFLY AI - EMA 6/24 + PARABOLIC SAR SCANNER"
    )
    logger.info("=" * 70)
    logger.info("Pairs: %d", len(OTC_PAIRS))
    logger.info("Timeframe: 1 minute")
    logger.info("EMA: 6 / 24")
    logger.info(
        "PSAR: step=%.2f max=%.2f",
        PSAR_STEP,
        PSAR_MAX,
    )
    logger.info(
        "SAR dots: %d-%d",
        MIN_SAR_DOTS,
        MAX_SAR_DOTS,
    )
    logger.info(
        "EMA cross age: %d-%d candles",
        MIN_CROSS_AGE,
        MAX_CROSS_AGE,
    )
    logger.info(
        "ATR spike filter: %.2fx baseline",
        ATR_SPIKE_MULTIPLIER,
    )
    logger.info(
        "Body filter: %.2fx ATR",
        BODY_ATR_MULTIPLIER,
    )
    logger.info("Mode: SIGNAL ONLY")
    logger.info("=" * 70)

    connect_iq_option()
    refresh_otc_asset_mappings()

    available = [
        pair for pair in OTC_PAIRS
        if stable_api.OP_code.ACTIVES.get(pair) is not None
    ]

    if not available:
        logger.error(
            "No configured OTC pair is currently available."
        )
        return

    send_telegram(
        "🤖 <b>Firefly EMA/SAR Scanner Started</b>\n\n"
        f"📊 Available pairs: {len(available)}/{len(OTC_PAIRS)}\n"
        "⏱ Timeframe: 1 minute\n"
        "📈 EMA: 6 / 24\n"
        "⚙️ PSAR: 0.05 / 0.20\n"
        "🔢 SAR dots: 3-7\n"
        "🔀 EMA crossover: 1-3 candles\n"
        "🛡 Volatility filter: ON\n"
        "🔁 Trend-cycle cooldown: ON\n"
        "🤖 Mode: SIGNAL ONLY"
    )

    logger.info("SCANNER STARTED")

    while True:
        try:
            if not connection_alive():
                logger.warning(
                    "IQ Option connection lost. Reconnecting..."
                )
                connect_iq_option()
                refresh_otc_asset_mappings()

            started = time.time()
            scan_all_pairs()

            elapsed = time.time() - started
            time.sleep(
                max(1, POLL_INTERVAL - elapsed)
            )

        except KeyboardInterrupt:
            logger.info("Scanner stopped by user.")
            break

        except Exception as exc:
            logger.exception(
                "Main loop error: %s", exc
            )
            time.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    try:
        run_bot()
    except KeyboardInterrupt:
        print("\nFirefly scanner stopped.")
    except Exception as exc:
        logger.exception(
            "Fatal error: %s", exc
        )
