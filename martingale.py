import os
import sys
import time
import json
import logging
import threading
import urllib.parse
import urllib.request
import concurrent.futures

from datetime import datetime, timedelta, timezone

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


# ============================================================
# TRADING CONFIGURATION
# ============================================================

TIMEFRAME = 60

# Signal:
#
# 7 consecutive RED candles   -> CALL
# 7 consecutive GREEN candles -> PUT

TARGET_STREAK = 7


# ============================================================
# MARTINGALE STAKES
# ============================================================

# Level 0 = Base
# Level 1 = Martingale 1
# Level 2 = Martingale 2
# Level 3 = Martingale 3
#
# Maximum of 4 trades in one sequence.
#
# User-requested progression:
#
# $118
# $256
# $558
# $1216
#
# Total maximum exposure:
#
# $2148

STAKE_SEQUENCE = [
    118,
    256,
    558,
    1216
]

BASE_STAKE = STAKE_SEQUENCE[0]

MAX_MARTINGALE_LEVELS = 3


# ============================================================
# 8 FAVOURITE OTC PAIRS
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
# GLOBAL TRADE LOCK
# ============================================================

# Ensures that only ONE complete trade/martingale sequence
# can run at a time.

global_trade_active = False

global_trade_lock = threading.Lock()

order_execution_lock = threading.Lock()


# ============================================================
# VALIDATION & LOGGING
# ============================================================

for var_name, val in [
    ("IQ_EMAIL", IQ_EMAIL),
    ("IQ_PASSWORD", IQ_PASSWORD),
    ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
    ("TELEGRAM_CHAT_ID", TELEGRAM_CHAT_ID),
]:

    if not val:
        print(f"ERROR: {var_name} is missing from .env")
        sys.exit(1)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger("FIREFLY-TRADER")

API = None


# ============================================================
# TIME / TIMEZONE
# ============================================================

# Nigeria uses West Africa Time (UTC+1).

WAT = timezone(timedelta(hours=1))


def get_wat_time():
    """
    Return the current local time in Nigeria.
    """

    return datetime.now(WAT)


def format_trade_time(dt=None):
    """
    Format a trade timestamp for Telegram notifications.

    Example:
        13:56:24 WAT
    """

    if dt is None:
        dt = get_wat_time()

    return dt.strftime("%H:%M:%S WAT")


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram_message(message):
    """
    Send an HTML-formatted Telegram message.
    """

    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }).encode("utf-8")

    try:

        req = urllib.request.Request(
            url,
            data=payload,
            method="POST"
        )

        with urllib.request.urlopen(
            req,
            timeout=10
        ) as response:

            res = json.loads(
                response.read().decode("utf-8")
            )

            if not res.get("ok"):

                logger.error(
                    "Telegram API rejection: %s",
                    res
                )

    except Exception as exc:

        logger.error(
            "Failed to send Telegram notification: %s",
            exc
        )


# ============================================================
# CONNECTION
# ============================================================

def connect_iq_option():

    global API

    logger.info("Connecting to IQ Option...")

    API = IQ_Option(
        IQ_EMAIL,
        IQ_PASSWORD
    )

    check, reason = API.connect()

    if not check:

        logger.error(
            "Connection failed: %s",
            reason
        )

        return False

    API.change_balance("PRACTICE")

    balance = API.get_balance()

    logger.info(
        "Connected successfully. Practice balance: $%.2f",
        balance
    )

    return True


# ============================================================
# CONNECTION CHECK
# ============================================================

def ensure_connection():

    global API

    try:

        if API is None or not API.check_connect():

            logger.warning(
                "Socket connection lost. Reconnecting..."
            )

            success = connect_iq_option()

            if success:

                refresh_otc_mappings()

                return True

            return False

        return True

    except Exception as e:

        logger.error(
            "Exception during connection check: %s",
            e
        )

        return connect_iq_option()


# ============================================================
# OTC ASSET MAPPING
# ============================================================

def refresh_otc_mappings():

    if API is None:
        return False

    try:

        API.api.api_option_init_all_result = None

        API.api.get_api_option_init_all()

        start = time.time()

        while (
            API.api.api_option_init_all_result is None
            and time.time() - start < 30
        ):

            time.sleep(0.1)

        result = API.api.api_option_init_all_result

        if result and result.get("isSuccessful"):

            result_data = result.get(
                "result",
                {}
            )

            for market_type in (
                "binary",
                "turbo"
            ):

                actives = (
                    result_data
                    .get(market_type, {})
                    .get("actives", {})
                )

                for active_id, active_data in actives.items():

                    raw_name = active_data.get(
                        "name",
                        ""
                    )

                    asset_name = (
                        raw_name.split(".", 1)[1]
                        if "." in raw_name
                        else raw_name
                    )

                    if asset_name in OTC_PAIRS:

                        stable_api.OP_code.ACTIVES[
                            asset_name
                        ] = int(active_id)

                        logger.info(
                            "Mapped asset %s -> ID %s",
                            asset_name,
                            active_id
                        )

            return True

    except Exception as e:

        logger.error(
            "Failed to refresh OTC mappings: %s",
            e
        )

    return False


# ============================================================
# BALANCE HELPERS
# ============================================================

def get_current_balance():
    """
    Retrieve the current IQ Option account balance.

    Returns:
        float | None
    """

    try:

        if API is not None:

            return API.get_balance()

    except Exception as e:

        logger.error(
            "Failed to retrieve current account balance: %s",
            e
        )

    return None


def format_balance(balance):
    """
    Format account balance for Telegram.

    Example:
        $12,345.67
    """

    if balance is None:

        return "Unavailable"

    return f"${balance:,.2f}"


# ============================================================
# DIRECTION ICON
# ============================================================

def get_direction_icon(direction):
    """
    Return the requested direction icon.

    CALL -> 🟢
    PUT  -> 🔴
    """

    if direction.lower() == "call":

        return "🟢"

    return "🔴"


# ============================================================
# ORDER EXECUTION
# ============================================================

def place_order_with_timeout(
    pair,
    stake,
    direction,
    duration=1
):

    def _execute():

        if not ensure_connection():

            return False, None

        active_id = (
            stable_api.OP_code.ACTIVES.get(pair)
        )

        if not active_id:

            logger.error(
                "No active ID found for %s",
                pair
            )

            return False, None

        with order_execution_lock:

            status, order_id = API.buy(
                stake,
                pair,
                direction,
                duration
            )

            return status, order_id

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=1
    ) as executor:

        future = executor.submit(_execute)

        try:

            return future.result(
                timeout=6.0
            )

        except concurrent.futures.TimeoutError:

            logger.error(
                "Order placement timed out for %s.",
                pair
            )

            return False, None

        except Exception as e:

            logger.error(
                "Exception during order execution: %s",
                e
            )

            return False, None


# ============================================================
# EXECUTE MARTINGALE SEQUENCE
# ============================================================

def execute_trade_sequence(
    pair,
    direction
):
    """
    Executes:

        Level 0: $1.18
        Level 1: $2.56
        Level 2: $5.58
        Level 3: $12.16

    A win at ANY level ends the sequence.

    A loss moves to the next level.

    Every Telegram notification contains:

        - Market
        - Direction
        - Stake
        - Level where applicable
        - Time
        - Current account balance

    Direction icons:

        CALL -> 🟢
        PUT  -> 🔴
    """

    global global_trade_active

    direction = direction.lower()

    direction_icon = get_direction_icon(
        direction
    )

    try:

        if not ensure_connection():

            logger.error(
                "Could not recover connection."
            )

            return

        # ========================================================
        # MARTINGALE LOOP
        # ========================================================

        for level, stake in enumerate(
            STAKE_SEQUENCE
        ):

            # ====================================================
            # LEVEL DESCRIPTION
            # ====================================================

            if level == 0:

                level_name = "BASE"

            else:

                level_name = (
                    f"MARTINGALE {level}"
                )

            # ====================================================
            # BALANCE BEFORE TRADE
            # ====================================================

            balance_before = get_current_balance()

            logger.info(
                "%s | %s | Direction: %s | "
                "Stake: $%.2f | Balance: $%.2f",

                pair,

                level_name,

                direction.upper(),

                stake,

                balance_before
                if balance_before is not None
                else 0
            )

            # ====================================================
            # PLACE TRADE
            # ====================================================

            check, order_id = (
                place_order_with_timeout(
                    pair,
                    stake,
                    direction,
                    1
                )
            )

            # ====================================================
            # ORDER FAILED
            # ====================================================

            if not check or not order_id:

                logger.error(
                    "%s order failed on %s",
                    level_name,
                    pair
                )

                current_balance = (
                    get_current_balance()
                )

                send_telegram_message(
                    f"❌ <b>{level_name} ORDER FAILED</b>\n"
                    f"Market: <b>{pair}</b>\n"
                    f"Direction: "
                    f"<b>{direction_icon} "
                    f"{direction.upper()}</b>\n"
                    f"Stake: <b>${stake:.2f}</b>\n"
                    f"💰 Balance: "
                    f"<b>{format_balance(current_balance)}</b>"
                )

                return

            # ====================================================
            # ACTUAL ORDER PLACEMENT TIME
            # ====================================================

            placed_at = get_wat_time()

            placed_time = format_trade_time(
                placed_at
            )

            logger.info(
                "%s order placed successfully. "
                "Order ID: %s | Placed: %s",

                level_name,

                order_id,

                placed_time
            )

            # ====================================================
            # BALANCE AFTER ORDER PLACEMENT
            # ====================================================

            balance_after_placement = (
                get_current_balance()
            )

            # ====================================================
            # BASE TRADE NOTIFICATION
            # ====================================================

            if level == 0:

                send_telegram_message(

                    f"📝 <b>BASE TRADE</b>\n"

                    f"Market: <b>{pair}</b>\n"

                    f"Direction: "
                    f"<b>{direction_icon} "
                    f"{direction.upper()}</b>\n"

                    f"Stake: "
                    f"<b>${stake:.2f}</b>\n"

                    f"Level: <b>0 / 3</b>\n"

                    f"🕐 Placed: "
                    f"<b>{placed_time}</b>\n"

                    f"💰 Balance: "
                    f"<b>"
                    f"{format_balance(balance_after_placement)}"
                    f"</b>"
                )

            # ====================================================
            # MARTINGALE TRADE NOTIFICATION
            # ====================================================

            else:

                send_telegram_message(

                    f"⚠️ <b>MARTINGALE {level}</b>\n"

                    f"Market: <b>{pair}</b>\n"

                    f"Direction: "
                    f"<b>{direction_icon} "
                    f"{direction.upper()}</b>\n"

                    f"Stake: "
                    f"<b>${stake:.2f}</b>\n"

                    f"Level: "
                    f"<b>{level} / 3</b>\n"

                    f"🕐 Placed: "
                    f"<b>{placed_time}</b>\n"

                    f"💰 Balance: "
                    f"<b>"
                    f"{format_balance(balance_after_placement)}"
                    f"</b>"
                )

            # ====================================================
            # WAIT FOR 1-MINUTE OPTION TO CLOSE
            # ====================================================

            time.sleep(61)

            # ====================================================
            # CAPTURE CLOSING TIME
            # ====================================================

            closed_at = get_wat_time()

            closed_time = format_trade_time(
                closed_at
            )

            # ====================================================
            # BALANCE AFTER TRADE
            # ====================================================

            balance_after = (
                get_current_balance()
            )

            if (
                balance_before is not None
                and balance_after is not None
            ):

                balance_diff = (
                    balance_after
                    - balance_before
                )

            else:

                balance_diff = 0

            logger.info(
                "%s settlement -> "
                "Pre: $%.2f | "
                "Post: $%.2f | "
                "Diff: $%.2f | "
                "Closed: %s",

                level_name,

                balance_before
                if balance_before is not None
                else 0,

                balance_after
                if balance_after is not None
                else 0,

                balance_diff,

                closed_time
            )

            # ====================================================
            # WIN
            # ====================================================

            if balance_diff > 0:

                logger.info(
                    "%s WIN on %s | Closed: %s",
                    level_name,
                    pair,
                    closed_time
                )

                send_telegram_message(

                    f"✅ <b>{level_name} WIN</b>\n"

                    f"Market: <b>{pair}</b>\n"

                    f"Direction: "
                    f"<b>{direction_icon} "
                    f"{direction.upper()}</b>\n"

                    f"Stake: "
                    f"<b>${stake:.2f}</b>\n"

                    f"Profit: "
                    f"<b>+${balance_diff:.2f}</b>\n"

                    f"Sequence: "
                    f"<b>RECOVERED</b>\n"

                    f"🕐 Closed: "
                    f"<b>{closed_time}</b>\n"

                    f"💰 Balance: "
                    f"<b>"
                    f"{format_balance(balance_after)}"
                    f"</b>"
                )

                return

            # ====================================================
            # LOSS
            # ====================================================

            logger.warning(
                "%s LOSS on %s | "
                "Loss: $%.2f | Closed: %s",

                level_name,

                pair,

                abs(balance_diff),

                closed_time
            )

            # ----------------------------------------------------
            # LOSS NOTIFICATION
            # ----------------------------------------------------

            send_telegram_message(

                f"❌ <b>{level_name} LOSS</b>\n"

                f"Market: <b>{pair}</b>\n"

                f"Direction: "
                f"<b>{direction_icon} "
                f"{direction.upper()}</b>\n"

                f"Stake: "
                f"<b>${stake:.2f}</b>\n"

                f"Loss: "
                f"<b>-${abs(balance_diff):.2f}</b>\n"

                f"🕐 Closed: "
                f"<b>{closed_time}</b>\n"

                f"💰 Balance: "
                f"<b>"
                f"{format_balance(balance_after)}"
                f"</b>"
            )

            # ====================================================
            # FINAL LEVEL LOST
            # ====================================================

            if level == len(STAKE_SEQUENCE) - 1:

                # Retrieve balance again immediately before
                # sending the final sequence result.

                final_balance = (
                    get_current_balance()
                )

                send_telegram_message(

                    f"❌ <b>SEQUENCE LOST</b>\n"

                    f"Market: <b>{pair}</b>\n"

                    f"Direction: "
                    f"<b>{direction_icon} "
                    f"{direction.upper()}</b>\n"

                    f"All 4 levels lost.\n"

                    f"Maximum sequence exposure: "
                    f"<b>$21.48</b>\n"

                    f"🕐 Final Closed: "
                    f"<b>{closed_time}</b>\n"

                    f"💰 Balance: "
                    f"<b>"
                    f"{format_balance(final_balance)}"
                    f"</b>"
                )

                logger.error(
                    "Complete Martingale sequence "
                    "lost on %s.",
                    pair
                )

                return

            # ====================================================
            # PREPARE NEXT MARTINGALE
            # ====================================================

            next_stake = STAKE_SEQUENCE[
                level + 1
            ]

            current_balance = (
                get_current_balance()
            )

            send_telegram_message(

                f"🔄 <b>LOSS → NEXT MARTINGALE</b>\n"

                f"Market: <b>{pair}</b>\n"

                f"Direction: "
                f"<b>{direction_icon} "
                f"{direction.upper()}</b>\n"

                f"Previous Stake: "
                f"<b>${stake:.2f}</b>\n"

                f"Next Stake: "
                f"<b>${next_stake:.2f}</b>\n"

                f"Next Level: "
                f"<b>{level + 1} / 3</b>\n"

                f"🕐 Previous Closed: "
                f"<b>{closed_time}</b>\n"

                f"💰 Balance: "
                f"<b>"
                f"{format_balance(current_balance)}"
                f"</b>"
            )

    except Exception as e:

        logger.error(
            "Error in trade sequence for %s: %s",
            pair,
            e
        )

        current_balance = (
            get_current_balance()
        )

        send_telegram_message(

            f"❌ <b>TRADE SEQUENCE ERROR</b>\n"

            f"Market: <b>{pair}</b>\n"

            f"Direction: "
            f"<b>{direction_icon} "
            f"{direction.upper()}</b>\n"

            f"Error: {str(e)}\n"

            f"💰 Balance: "
            f"<b>"
            f"{format_balance(current_balance)}"
            f"</b>"
        )

    finally:

        with global_trade_lock:

            global_trade_active = False

            logger.info(
                "Global trading lock released. "
                "Bot ready for new signals."
            )


# ============================================================
# CANDLE STREAK ANALYZER
# ============================================================

def get_current_live_streak(candles):

    if not candles or len(candles) < 2:

        return "DOJI", 0

    color_streak = None

    count = 0

    # ========================================================
    # Exclude the currently-forming candle.
    #
    # candles[:-1] = completed candles
    # ========================================================

    for candle in reversed(
        candles[:-1]
    ):

        open_price = float(
            candle["open"]
        )

        close_price = float(
            candle["close"]
        )

        if close_price > open_price:

            candle_color = "GREEN"

        elif close_price < open_price:

            candle_color = "RED"

        else:

            candle_color = "DOJI"

        # ====================================================
        # DOJI BREAKS THE STREAK
        # ====================================================

        if color_streak is None:

            if candle_color == "DOJI":

                return "DOJI", 0

            color_streak = candle_color

            count = 1

        elif candle_color == color_streak:

            count += 1

        else:

            break

    return color_streak, count


# ============================================================
# MAIN BOT LOOP
# ============================================================

def run_bot():

    global global_trade_active

    # ========================================================
    # CONNECT
    # ========================================================

    if not connect_iq_option():

        return

    # ========================================================
    # MAP OTC PAIRS
    # ========================================================

    refresh_otc_mappings()

    # ========================================================
    # START MESSAGE
    # ========================================================

    total_exposure = sum(
        STAKE_SEQUENCE
    )

    current_balance = (
        get_current_balance()
    )

    send_telegram_message(

        f"🚀 <b>Firefly AI Bot Started</b>\n\n"

        f"📊 Monitoring: "
        f"<b>8 OTC pairs</b>\n"

        f"🎯 Target Streak: "
        f"<b>7 candles</b>\n"

        f"💰 Base Stake: "
        f"<b>$118</b>\n"

        f"🔄 Martingale Levels: "
        f"<b>3</b>\n"

        f"📈 Max Trades / Sequence: "
        f"<b>4</b>\n"

        f"💵 Maximum Exposure: "
        f"<b>${total_exposure:.2f}</b>\n"

        f"💰 Current Balance: "
        f"<b>{format_balance(current_balance)}</b>\n\n"

        f"🔴 7 RED → CALL\n"

        f"🟢 7 GREEN → PUT"
    )

    logger.info(
        "Bot running. Monitoring 8 OTC pairs..."
    )

    # ========================================================
    # PREVENT PROCESSING SAME CANDLE REPEATEDLY
    # ========================================================

    last_checked_timestamps = {
        pair: 0
        for pair in OTC_PAIRS
    }

    try:

        while True:

            # ====================================================
            # CONNECTION
            # ====================================================

            if not ensure_connection():

                time.sleep(5)

                continue

            # ====================================================
            # DON'T SCAN WHILE A TRADE SEQUENCE IS RUNNING
            # ====================================================

            with global_trade_lock:

                if global_trade_active:

                    time.sleep(2)

                    continue

            # ====================================================
            # SCAN ALL 8 PAIRS
            # ====================================================

            for pair in OTC_PAIRS:

                try:

                    # ============================================
                    # CHECK GLOBAL LOCK AGAIN
                    # ============================================

                    with global_trade_lock:

                        if global_trade_active:

                            break

                    # ============================================
                    # GET 20 CANDLES
                    # ============================================

                    candles = API.get_candles(
                        pair,
                        TIMEFRAME,
                        20,
                        time.time()
                    )

                    if not candles:

                        continue

                    # ============================================
                    # LATEST CANDLE
                    # ============================================

                    latest_candle = candles[-1]

                    candle_from = int(
                        latest_candle["from"]
                    )

                    # ============================================
                    # ONLY PROCESS NEW CANDLES
                    # ============================================

                    if (
                        candle_from
                        > last_checked_timestamps[pair]
                    ):

                        last_checked_timestamps[
                            pair
                        ] = candle_from

                        # ========================================
                        # ANALYZE STREAK
                        # ========================================

                        (
                            current_color,
                            streak_count
                        ) = get_current_live_streak(
                            candles
                        )

                        logger.info(
                            "Pair: %s | "
                            "Streak: %d %s",

                            pair,

                            streak_count,

                            current_color
                        )

                        # ========================================
                        # TARGET STREAK REACHED
                        # ========================================

                        if (
                            streak_count
                            >= TARGET_STREAK
                        ):

                            # ====================================
                            # LOCK TRADING
                            # ====================================

                            with global_trade_lock:

                                if global_trade_active:

                                    continue

                                global_trade_active = True

                            # ====================================
                            # RED -> CALL
                            # ====================================

                            if current_color == "RED":

                                logger.info(
                                    "4+ RED streak on %s. "
                                    "Triggering CALL.",
                                    pair
                                )

                                send_telegram_message(

                                    f"🔥 <b>4 RED STREAK</b>\n"

                                    f"Market: "
                                    f"<b>{pair}</b>\n"

                                    f"Streak: "
                                    f"<b>"
                                    f"{streak_count} RED"
                                    f"</b>\n"

                                    f"Signal: "
                                    f"<b>🟢 CALL</b>\n"

                                    f"Base Stake: "
                                    f"<b>$1.18</b>"
                                )

                                threading.Thread(
                                    target=execute_trade_sequence,
                                    args=(
                                        pair,
                                        "call"
                                    ),
                                    daemon=True
                                ).start()

                                break

                            # ====================================
                            # GREEN -> PUT
                            # ====================================

                            elif current_color == "GREEN":

                                logger.info(
                                    "4+ GREEN streak on %s. "
                                    "Triggering PUT.",
                                    pair
                                )

                                send_telegram_message(

                                    f"🔥 <b>4 GREEN STREAK</b>\n"

                                    f"Market: "
                                    f"<b>{pair}</b>\n"

                                    f"Streak: "
                                    f"<b>"
                                    f"{streak_count} GREEN"
                                    f"</b>\n"

                                    f"Signal: "
                                    f"<b>🔴 PUT</b>\n"

                                    f"Base Stake: "
                                    f"<b>$1.18</b>"
                                )

                                threading.Thread(
                                    target=execute_trade_sequence,
                                    args=(
                                        pair,
                                        "put"
                                    ),
                                    daemon=True
                                ).start()

                                break

                except Exception as ex:

                    logger.error(
                        "Error checking candles "
                        "for %s: %s",
                        pair,
                        ex
                    )

            # ====================================================
            # POLLING INTERVAL
            # ====================================================

            time.sleep(3)

    except KeyboardInterrupt:

        logger.info(
            "Bot stopped by user."
        )

        send_telegram_message(

            "⚠️ <b>Firefly AI Bot Stopped</b> "
            "by user command."
        )


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    run_bot()
