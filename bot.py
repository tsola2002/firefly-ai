import os
import sys
import time

from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option


# ============================================================
# FIREFLY AI - IQ OPTION TRADING BOT
# ============================================================
#
# Current stages:
#
# STAGE 1 - Environment & Authentication
# STAGE 2 - Account & Connection Verification
# STAGE 3 - Market Data / Candle Retrieval
#
# Current market:
# EUR/JPY OTC
#
# Current timeframe:
# 1-minute candles
#
# Current account:
# PRACTICE
#
# ============================================================


# ============================================================
# STAGE 1 - ENVIRONMENT & AUTHENTICATION
# ============================================================


# ------------------------------------------------------------
# 1. VERIFY PYTHON ENVIRONMENT
# ------------------------------------------------------------

print("=" * 60)
print("FIREFLY AI - IQ OPTION TRADING BOT")
print("=" * 60)

print("\n[STAGE 1] Environment & Authentication")

print("\nPython executable:")
print(sys.executable)

print("\nPython version:")
print(sys.version.split()[0])


# Make sure the project virtual environment is being used
if ".venv" not in sys.executable:

    print("\nERROR: The project .venv is NOT being used.")

    print("\nActivate the virtual environment with:")

    print("source .venv/Scripts/activate")

    sys.exit(1)


print("\nVirtual environment: OK")


# ------------------------------------------------------------
# 2. LOAD ENVIRONMENT VARIABLES
# ------------------------------------------------------------

print("\nLoading environment variables...")

load_dotenv()

email = os.getenv("IQ_EMAIL")
password = os.getenv("IQ_PASSWORD")


# Validate email
if not email:

    print("\nERROR: IQ_EMAIL is not set in your .env file.")

    sys.exit(1)


# Validate password
if not password:

    print("\nERROR: IQ_PASSWORD is not set in your .env file.")

    sys.exit(1)


print("Email loaded:", email)
print("Password loaded: YES")


# ============================================================
# STAGE 2 - CONNECTION & ACCOUNT VERIFICATION
# ============================================================


# ------------------------------------------------------------
# 3. CREATE API CONNECTION
# ------------------------------------------------------------

print("\n[STAGE 2] Connection & Account Verification")

print("\nConnecting to IQ Option...")

API = IQ_Option(email, password)


try:

    check, reason = API.connect()

    if not check:

        print("\nConnection failed:")
        print(reason)

        sys.exit(1)


    print("\nSuccessfully connected!")


    # --------------------------------------------------------
    # 4. SWITCH TO PRACTICE ACCOUNT
    # --------------------------------------------------------

    print("\nSwitching to PRACTICE account...")

    API.change_balance("PRACTICE")

    # Give the API a moment to update
    time.sleep(2)

    print("Account type: PRACTICE")


    # --------------------------------------------------------
    # 5. GET PRACTICE BALANCE
    # --------------------------------------------------------

    print("\nRetrieving practice balance...")

    balance = API.get_balance()

    print(f"Practice Balance: ${balance:.2f}")


    # --------------------------------------------------------
    # 6. CHECK CONNECTION STATUS
    # --------------------------------------------------------

    print("\nChecking connection status...")

    if API.check_connect():

        print("API connection: ACTIVE")

    else:

        print("API connection: LOST")

        sys.exit(1)


    # --------------------------------------------------------
    # 7. BASIC ACCOUNT TEST
    # --------------------------------------------------------

    print("\nAccount test completed successfully.")


except Exception as e:

    print("\nConnection/API error:")
    print(type(e).__name__, "-", str(e))

    print("\nFull traceback:")

    import traceback
    traceback.print_exc()

    sys.exit(1)


# ============================================================
# STAGE 3 - MARKET DATA
# ============================================================


# ------------------------------------------------------------
# 8. IDENTIFY EUR/JPY OTC
# ------------------------------------------------------------

print("\n" + "=" * 60)

print("[STAGE 3] MARKET DATA")

print("=" * 60)

print("\nChecking EUR/JPY OTC...")


# Candidate asset names
otc_assets = [
    "EURJPY-OTC",
    "EURJPY"
]


asset_found = None


for asset in otc_assets:

    print(f"\nTesting asset: {asset}")


    try:

        # ----------------------------------------------------
        # 9. RETRIEVE CANDLE DATA
        # ----------------------------------------------------
        #
        # get_candles parameters:
        #
        # asset
        # interval = 60 seconds = 1 minute
        # count = 5 candles
        # endtime = current Unix timestamp
        #
        # ----------------------------------------------------

        candles = API.get_candles(
            asset,
            60,
            5,
            time.time()
        )


        if candles:

            asset_found = asset

            print(f"SUCCESS: {asset} is available.")

            print(f"Candles received: {len(candles)}")

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
# 10. CHECK WHETHER AN ASSET WAS FOUND
# ------------------------------------------------------------

if not asset_found:

    print("\n" + "=" * 60)

    print("ERROR: EUR/JPY OTC candle data could not be retrieved.")

    print("=" * 60)

    sys.exit(1)


# ============================================================
# 11. DISPLAY RAW CANDLE DATA
# ============================================================

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


# ============================================================
# 12. STAGE 3 TEST COMPLETE
# ============================================================

print("\n" + "=" * 60)

print("STAGE 3 CANDLE RETRIEVAL COMPLETE")

print("=" * 60)

print(f"\nAsset: {asset_found}")
print("Timeframe: 1 minute")
print(f"Candles retrieved: {len(candles)}")

print("\nFirefly AI successfully retrieved market data.")


# ============================================================
# 13. CLEAN DISCONNECT
# ============================================================

print("\nDisconnecting from IQ Option...")

try:

    API.close()

    print("Disconnected successfully.")

except Exception as e:

    print(
        "Disconnect warning:",
        type(e).__name__,
        "-",
        str(e)
    )


print("\nFirefly AI Stage 3 test completed.")