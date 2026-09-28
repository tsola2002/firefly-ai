import os
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option

load_dotenv()

IQ_EMAIL = os.getenv("IQ_EMAIL")
IQ_PASSWORD = os.getenv("IQ_PASSWORD")

LOCAL_TIMEZONE = timezone(timedelta(hours=1))

TEST_DATES = [
    "2026-09-22",
    "2026-09-01",
    "2026-08-01",
    "2026-07-01",
    "2026-06-01",
    "2026-05-01",
    "2026-04-01",
    "2026-03-01",
    "2026-02-01",
    "2026-01-01",
]

PAIRS = [
    "USDJPY-OTC",
    "AUDUSD-OTC",
    "EURJPY-OTC",
    "AUDJPY-OTC",
    "GBPJPY-OTC",
    "GBPAUD-OTC",
    "GBPCAD-OTC",
    "CADJPY-OTC",
]

api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)

check, reason = api.connect()

if not check:
    print("Connection failed:", reason)
    raise SystemExit(1)

api.change_balance("PRACTICE")

print("\nIQ OPTION HISTORICAL OTC AVAILABILITY TEST")
print("=" * 80)

for date_string in TEST_DATES:

    year, month, day = map(int, date_string.split("-"))

    local_start = datetime(
        year,
        month,
        day,
        0,
        0,
        0,
        tzinfo=LOCAL_TIMEZONE
    )

    local_end = local_start + timedelta(days=1)

    end_timestamp = int(local_end.timestamp())

    print(f"\nDATE: {date_string}")
    print("-" * 80)

    for pair in PAIRS:

        try:

            candles = api.get_candles(
                pair,
                60,
                10,
                end_timestamp
            )

            print(
                f"{pair:<15} -> "
                f"{len(candles) if candles else 0} candles"
            )

        except Exception as exc:

            print(
                f"{pair:<15} -> ERROR: {exc}"
            )

        time.sleep(0.3)