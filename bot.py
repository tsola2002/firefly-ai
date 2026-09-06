import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# FIREFLY AI - IQ OPTION TRADING BOT
# ============================================================

print("=" * 60)
print("FIREFLY AI - IQ OPTION TRADING BOT")
print("=" * 60)


# ============================================================
# STAGE 1 - ENVIRONMENT & AUTHENTICATION
# ============================================================

print("\n[STAGE 1] Environment & Authentication")
print()


# ------------------------------------------------------------
# Verify Python environment
# ------------------------------------------------------------

print("Python executable:")
print(sys.executable)

print("\nPython version:")
print(
    f"{sys.version_info.major}."
    f"{sys.version_info.minor}."
    f"{sys.version_info.micro}"
)

# Check that we are running inside the virtual environment
if ".venv" in sys.executable:
    print("\nVirtual environment: OK")
else:
    print("\nWARNING: Virtual environment may not be active.")


# ------------------------------------------------------------
# Load environment variables
# ------------------------------------------------------------

print("\nLoading environment variables...")

load_dotenv()

email = os.getenv("IQ_EMAIL")
password = os.getenv("IQ_PASSWORD")


if not email:
    print("\nERROR: IQ_EMAIL was not found in .env")
    sys.exit(1)

if not password:
    print("\nERROR: IQ_PASSWORD was not found in .env")
    sys.exit(1)


print(f"Email loaded: {email}")
print("Password loaded: YES")


# ============================================================
# STAGE 2 - CONNECTION & ACCOUNT VERIFICATION
# ============================================================

print("\n[STAGE 2] Connection & Account Verification")
print()

print("Connecting to IQ Option...")


API = IQ_Option(email, password)


try:

    # --------------------------------------------------------
    # Connect to IQ Option
    # --------------------------------------------------------

    check, reason = API.connect()

    if not check:
        print("\nConnection failed:")
        print(reason)
        sys.exit(1)

    print("\nSuccessfully connected!")


    # --------------------------------------------------------
    # Switch to PRACTICE account
    # --------------------------------------------------------

    print("\nSwitching to PRACTICE account...")

    API.change_balance("PRACTICE")

    time.sleep(2)

    print("Account type: PRACTICE")


    # --------------------------------------------------------
    # Retrieve practice balance
    # --------------------------------------------------------

    print("\nRetrieving practice balance...")

    balance = API.get_balance()

    print(f"Practice Balance: ${balance:.2f}")


    # --------------------------------------------------------
    # Check connection status
    # --------------------------------------------------------

    print("\nChecking connection status...")

    if API.check_connect():

        print("API connection: ACTIVE")

    else:

        print("API connection: LOST")
        sys.exit(1)


    print("\nAccount test completed successfully.")


except Exception as e:

    print("\nConnection/API error:")

    print(
        type(e).__name__,
        "-",
        str(e)
    )

    print("\nFull traceback:")

    import traceback

    traceback.print_exc()

    sys.exit(1)


# ============================================================
# STAGE 3 - MARKET DATA RETRIEVAL
# ============================================================

print("\n" + "=" * 60)
print("[STAGE 3] MARKET DATA RETRIEVAL")
print("=" * 60)


# ------------------------------------------------------------
# EUR/JPY OTC
# ------------------------------------------------------------

print("\nChecking EUR/JPY OTC...")

# We test both names because the available asset name
# can differ depending on the IQ Option market/session.

otc_assets = [
    "EURJPY-OTC",
    "EURJPY"
]


asset_found = None
candles = []


# ------------------------------------------------------------
# Retrieve 5 one-minute candles
# ------------------------------------------------------------

for asset in otc_assets:

    print(f"\nTesting asset: {asset}")

    try:

        candles = API.get_candles(
            asset,
            60,             # 60 seconds = 1 minute
            5,              # Number of candles
            time.time()
        )


        if candles:

            asset_found = asset

            print(
                f"SUCCESS: {asset} is available."
            )

            print(
                f"Candles received: {len(candles)}"
            )

            break

        else:

            print(
                f"No candle data returned for {asset}."
            )


    except Exception as e:

        print(
            f"Could not retrieve data for {asset}: "
            f"{type(e).__name__} - {str(e)}"
        )


# ------------------------------------------------------------
# Verify asset
# ------------------------------------------------------------

if not asset_found:

    print("\nERROR: Could not retrieve candle data.")

    print(
        "Please check the available IQ Option asset "
        "name and market status."
    )

    sys.exit(1)


# ------------------------------------------------------------
# Display raw candle data
# ------------------------------------------------------------

print("\n" + "=" * 60)
print("RAW CANDLE DATA")
print("=" * 60)

print(f"\nAsset: {asset_found}")
print("Timeframe: 1 minute")
print(f"Number of candles: {len(candles)}")


for index, candle in enumerate(candles, start=1):

    print(f"\nCANDLE {index}")
    print("-" * 40)

    print(candle)


print("\n" + "=" * 60)
print("STAGE 3 CANDLE RETRIEVAL COMPLETE")
print("=" * 60)

print(f"\nAsset: {asset_found}")
print("Timeframe: 1 minute")
print(f"Candles retrieved: {len(candles)}")

print("\nFirefly AI successfully retrieved market data.")


# ============================================================
# STAGE 4 - CANDLE DATA PROCESSING
# ============================================================

print("\n" + "=" * 60)
print("[STAGE 4] CANDLE DATA PROCESSING")
print("=" * 60)


# ------------------------------------------------------------
# Candle processing function
# ------------------------------------------------------------

def process_candle(candle):
    """
    Convert a raw IQ Option candle into a cleaner structure.

    Raw IQ Option candle:

        {
            'from': timestamp,
            'open': price,
            'close': price,
            'min': price,
            'max': price,
            'volume': volume
        }

    Processed candle:

        {
            'time': readable time,
            'open': open price,
            'high': high price,
            'low': low price,
            'close': close price,
            'type': GREEN or RED
        }
    """


    # --------------------------------------------------------
    # Extract values
    # --------------------------------------------------------

    timestamp = candle.get("from")

    open_price = candle.get("open")
    close_price = candle.get("close")
    low_price = candle.get("min")
    high_price = candle.get("max")


    # --------------------------------------------------------
    # Validate required values
    # --------------------------------------------------------

    if timestamp is None:
        raise ValueError("Candle is missing timestamp.")

    if (
        open_price is None
        or close_price is None
        or low_price is None
        or high_price is None
    ):
        raise ValueError(
            "Candle is missing OHLC price data."
        )


    # --------------------------------------------------------
    # Convert timestamp
    # --------------------------------------------------------

    candle_time = datetime.fromtimestamp(
        timestamp
    ).strftime("%H:%M:%S")


    # --------------------------------------------------------
    # Determine candle color/type
    # --------------------------------------------------------

    if close_price > open_price:

        candle_type = "GREEN"

    elif close_price < open_price:

        candle_type = "RED"

    else:

        # Open and close are identical.
        # This is a Doji candle.

        candle_type = "DOJI"


    # --------------------------------------------------------
    # Return processed candle
    # --------------------------------------------------------

    return {
        "time": candle_time,
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "type": candle_type
    }


# ------------------------------------------------------------
# Process all candles
# ------------------------------------------------------------

processed_candles = []


print("\nProcessing candles...")


for candle in candles:

    try:

        processed = process_candle(candle)

        processed_candles.append(processed)

    except Exception as e:

        print(
            "\nWARNING: Could not process candle:"
        )

        print(
            type(e).__name__,
            "-",
            str(e)
        )


# ------------------------------------------------------------
# Verify processed candles
# ------------------------------------------------------------

if not processed_candles:

    print(
        "\nERROR: No candles could be processed."
    )

    sys.exit(1)


# ============================================================
# DISPLAY PROCESSED CANDLE DATA
# ============================================================

print("\n" + "=" * 90)
print("PROCESSED CANDLE DATA")
print("=" * 90)

print(
    f"\nAsset: {asset_found}"
)

print(
    "Timeframe: 1 minute"
)

print(
    f"Candles processed: {len(processed_candles)}"
)

print()


# ------------------------------------------------------------
# Table header
# ------------------------------------------------------------

print(
    f"{'TIME':<12}"
    f"{'OPEN':>12}"
    f"{'HIGH':>12}"
    f"{'LOW':>12}"
    f"{'CLOSE':>12}"
    f"{'TYPE':>10}"
)

print("-" * 70)


# ------------------------------------------------------------
# Display each processed candle
# ------------------------------------------------------------

for candle in processed_candles:

    print(
        f"{candle['time']:<12}"
        f"{candle['open']:>12.4f}"
        f"{candle['high']:>12.4f}"
        f"{candle['low']:>12.4f}"
        f"{candle['close']:>12.4f}"
        f"{candle['type']:>10}"
    )


# ============================================================
# STAGE 4 SUMMARY
# ============================================================

print("\n" + "=" * 90)
print("STAGE 4 CANDLE PROCESSING COMPLETE")
print("=" * 90)


# Count candle types

green_count = sum(
    1
    for candle in processed_candles
    if candle["type"] == "GREEN"
)

red_count = sum(
    1
    for candle in processed_candles
    if candle["type"] == "RED"
)

doji_count = sum(
    1
    for candle in processed_candles
    if candle["type"] == "DOJI"
)


print(
    f"\nGREEN candles: {green_count}"
)

print(
    f"RED candles:   {red_count}"
)

print(
    f"DOJI candles:  {doji_count}"
)

print(
    f"Total candles: {len(processed_candles)}"
)


print(
    "\nFirefly AI successfully processed "
    "the market data."
)


# ============================================================
# END OF PROGRAM
# ============================================================

print("\n" + "=" * 60)
print("FIREFLY AI STAGE 4 TEST COMPLETED")
print("=" * 60)

print(
    "\nConnection will be left to terminate naturally."
)

print("\nProgram finished.")

