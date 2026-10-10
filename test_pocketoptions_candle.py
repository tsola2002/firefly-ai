import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from pocketoptionapi import PocketOption


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

if load_dotenv:
    load_dotenv(ENV_FILE)

PO_SSID = os.getenv("PO_SSID")

TIMEFRAME = 60
LOOKBACK_SECONDS = 60 * 60  # Request approximately the last hour

FAVORITE_PAIRS = [
    "USDJPY",
    "AUDUSD",
    "EURJPY",
    "AUDJPY",
    "GBPJPY",
    "GBPAUD",
    "GBPCAD",
    "CADJPY",
]

# A known OTC stock asset to help diagnose whether candle retrieval
# works for any asset at all.
CONTROL_ASSET = "#AAPL_otc"

LAGOS = timezone(timedelta(hours=1), name="WAT")


# ============================================================
# HELPERS
# ============================================================

def lagos_datetime(timestamp):
    """Convert a Unix timestamp into Nigeria local time."""
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).astimezone(LAGOS)


def print_candle(candle):
    """Print a candle while tolerating different API field names."""
    if not isinstance(candle, dict):
        print(candle)
        return

    timestamp = (
        candle.get("time")
        or candle.get("timestamp")
        or candle.get("from")
        or candle.get("at")
    )

    if timestamp is not None:
        try:
            readable_time = lagos_datetime(timestamp).strftime(
                "%Y-%m-%d %H:%M:%S WAT"
            )
        except (TypeError, ValueError, OSError):
            readable_time = str(timestamp)
    else:
        readable_time = "Unknown time"

    open_price = candle.get("open")
    close_price = candle.get("close")
    high_price = candle.get("high")
    low_price = candle.get("low")

    if open_price is not None and close_price is not None:
        if close_price > open_price:
            color = "GREEN"
        elif close_price < open_price:
            color = "RED"
        else:
            color = "DOJI"
    else:
        color = "UNKNOWN"

    print(
        f"Time: {readable_time} | "
        f"Open: {open_price} | "
        f"High: {high_price} | "
        f"Low: {low_price} | "
        f"Close: {close_price} | "
        f"Color: {color}"
    )


def normalize_asset_name(name):
    """Normalize asset symbols for matching currency-pair names."""
    return str(name).upper().replace("-", "_").replace("/", "").strip()


def find_matching_assets(assets):
    """Find available OTC assets matching the favorite currency pairs."""
    matches = {}

    if not isinstance(assets, dict):
        return matches

    for pair in FAVORITE_PAIRS:
        matching_symbols = []

        for symbol, info in assets.items():
            normalized = normalize_asset_name(symbol)
            normalized_pair = pair.replace("/", "").upper()

            if normalized_pair not in normalized:
                continue

            if "OTC" not in normalized:
                continue

            if not isinstance(info, dict):
                info = {}

            matching_symbols.append((symbol, info))

        matches[pair] = matching_symbols

    return matches


def request_candles(api, asset, server_timestamp):
    """Request recent historical candles and return the result."""
    start_time = int(server_timestamp - LOOKBACK_SECONDS)

    print(f"\nRequesting candles for: {asset}")
    print(f"Timeframe: {TIMEFRAME} seconds")
    print(f"Start time: {lagos_datetime(start_time)}")
    print(f"Server time: {lagos_datetime(server_timestamp)}")

    try:
        candles = api.get_historical_candles(
            asset,
            TIMEFRAME,
            start_time=start_time,
            offset=LOOKBACK_SECONDS,
            count_request=1,
        )
    except Exception as exc:
        print(f"Historical candle request failed: {type(exc).__name__}: {exc}")
        return []

    if not candles:
        print("No historical candles returned.")
        return []

    if not isinstance(candles, (list, tuple)):
        print("Unexpected candle response type:", type(candles).__name__)
        print("Response:", repr(candles)[:1500])
        return []

    print(f"Candles returned: {len(candles)}")

    for candle in candles[-10:]:
        print_candle(candle)

    if len(candles) > 10:
        print(f"... showing the latest 10 of {len(candles)} candles")

    return list(candles)


# ============================================================
# MAIN DIAGNOSTIC
# ============================================================

def main():
    if not PO_SSID:
        print("ERROR: PO_SSID was not found.")
        print(f"Check that your .env file exists at: {ENV_FILE}")
        print("It should contain PO_SSID=your_session_value")
        return

    api = PocketOption(PO_SSID)
    connected = False

    try:
        # ----------------------------------------------------
        # CONNECT
        # ----------------------------------------------------
        print("=" * 65)
        print("POCKET OPTION CONNECTION TEST")
        print("=" * 65)

        result = api.connect()

        if isinstance(result, tuple):
            connected = bool(result[0])
            error_message = result[1] if len(result) > 1 else None
        else:
            connected = bool(result)
            error_message = None

        print("Connected:", connected)
        print("Connection message:", error_message)

        if not connected:
            print("Connection failed. Check your session and network.")
            return

        # ----------------------------------------------------
        # SERVER TIME
        # ----------------------------------------------------
        print("\n" + "=" * 65)
        print("SERVER TIME CHECK")
        print("=" * 65)

        server_timestamp = None

        try:
            server_timestamp = api.get_server_timestamp()
        except Exception as exc:
            print("Could not get synchronized server time:", exc)

        try:
            native_timestamp = api.get_server_native_time()
        except Exception as exc:
            native_timestamp = None
            print("Could not get native server time:", exc)

        print("Synchronized timestamp:", server_timestamp)
        print("Native timestamp:", native_timestamp)

        if server_timestamp is not None:
            print(
                "Server datetime (Nigeria):",
                lagos_datetime(server_timestamp),
            )

        if server_timestamp is not None and native_timestamp is not None:
            try:
                print(
                    "Timestamp difference:",
                    round(float(server_timestamp) - float(native_timestamp), 3),
                    "seconds",
                )
            except (TypeError, ValueError):
                pass

        if server_timestamp is None:
            print("ERROR: Cannot retrieve server time; skipping candle requests.")
            return

        # ----------------------------------------------------
        # ASSET LIST
        # ----------------------------------------------------
        print("\n" + "=" * 65)
        print("ASSET CHECK")
        print("=" * 65)

        try:
            assets = api.get_assets()
        except Exception as exc:
            print("Could not retrieve assets:", type(exc).__name__, exc)
            assets = {}

        print("Assets response type:", type(assets).__name__)
        print("Total assets:", len(assets) if isinstance(assets, dict) else 0)

        if not isinstance(assets, dict) or not assets:
            print("No asset dictionary returned. Cannot identify valid symbols.")
            return

        matches = find_matching_assets(assets)

        print("\nMatching favorite OTC currency pairs:")

        for pair, symbols in matches.items():
            if not symbols:
                print(f"{pair}: NO MATCH FOUND")
                continue

            for symbol, info in symbols:
                print(
                    f"{pair} -> {symbol}"
                    f" | category: {info.get('category', 'unknown')}"
                    f" | available: {info.get('is_available', 'unknown')}"
                    f" | id: {info.get('id', 'unknown')}"
                    f" | payout: {info.get('payout', 'unknown')}"
                )

        # ----------------------------------------------------
        # CONTROL ASSET TEST
        # ----------------------------------------------------
        print("\n" + "=" * 65)
        print("CONTROL ASSET TEST")
        print("=" * 65)

        if CONTROL_ASSET in assets:
            control_info = assets[CONTROL_ASSET]
            print("Control asset:", CONTROL_ASSET)
            print("Control asset details:", control_info)

            if isinstance(control_info, dict) and not control_info.get(
                "is_available", False
            ):
                print(
                    "WARNING: The control asset exists but is not marked available."
                )

            try:
                subscribe_result = api.subscribe(CONTROL_ASSET, period=TIMEFRAME)
                print("Control asset subscription result:", subscribe_result)
            except Exception as exc:
                print("Control asset subscription error:", type(exc).__name__, exc)

            control_candles = request_candles(
                api,
                CONTROL_ASSET,
                float(server_timestamp),
            )
        else:
            print(
                f"{CONTROL_ASSET} was not found in the current asset list. "
                "Skipping the control test."
            )
            control_candles = []

        # ----------------------------------------------------
        # CURRENCY PAIR TESTS
        # ----------------------------------------------------
        print("\n" + "=" * 65)
        print("FAVORITE OTC CURRENCY CANDLE TESTS")
        print("=" * 65)

        results = {}

        for pair, symbols in matches.items():
            available_symbols = [
                (symbol, info)
                for symbol, info in symbols
                if isinstance(info, dict)
                and info.get("is_available") is True
            ]

            if not available_symbols:
                print(f"\n{pair}: No matching available OTC symbol.")
                results[pair] = 0
                continue

            # Test each matching symbol marked available until one
            # returns historical candles.
            pair_candles = []

            for symbol, info in available_symbols:
                try:
                    subscribe_result = api.subscribe(
                        symbol,
                        period=TIMEFRAME,
                    )
                    print(f"\nSubscribe {symbol}: {subscribe_result}")
                except Exception as exc:
                    print(
                        f"\nSubscription error for {symbol}: "
                        f"{type(exc).__name__}: {exc}"
                    )

                pair_candles = request_candles(
                    api,
                    symbol,
                    float(server_timestamp),
                )

                if pair_candles:
                    print(f"SUCCESS: Historical candles received for {symbol}.")
                    break

            results[pair] = len(pair_candles)

        # ----------------------------------------------------
        # SUMMARY
        # ----------------------------------------------------
        print("\n" + "=" * 65)
        print("DIAGNOSTIC SUMMARY")
        print("=" * 65)

        print(
            f"Control asset ({CONTROL_ASSET}): "
            f"{len(control_candles)} candles"
        )

        for pair, count in results.items():
            print(f"{pair}: {count} candles")

        if control_candles and not any(results.values()):
            print(
                "\nDIAGNOSIS: Historical retrieval works for the control asset, "
                "but not for the tested currency pairs. Check the exact currency "
                "asset symbols and whether their historical data is supported."
            )
        elif not control_candles and not any(results.values()):
            print(
                "\nDIAGNOSIS: No candles were returned for the control asset "
                "or the tested currency pairs. The problem may be the history "
                "request parameters, API/library behavior, or server response. "
                "The successful subscription alone does not confirm that "
                "historical candles are available."
            )
        elif any(results.values()):
            print(
                "\nDIAGNOSIS: At least one currency pair returned historical "
                "candles. Use the exact successful symbol in your report script."
            )

    except KeyboardInterrupt:
        print("\nStopped by user.")

    except Exception as exc:
        print(f"\nUnexpected error: {type(exc).__name__}: {exc}")

    finally:
        if connected:
            print("\nDisconnecting from Pocket Option...")
            try:
                disconnected = api.disconnect_websocket(timeout=12.0)
                print("Disconnect result:", disconnected)
            except Exception as exc:
                print("Disconnect warning:", type(exc).__name__, exc)

        print("Diagnostic finished.")


if __name__ == "__main__":
    main()