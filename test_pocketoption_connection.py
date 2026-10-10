
import os
from pathlib import Path

from dotenv import load_dotenv
from pocketoptionapi import PocketOption

load_dotenv(Path(__file__).resolve().parent / ".env")

ssid = os.getenv("PO_SSID", "").strip()

if not ssid:
    raise SystemExit("PO_SSID is missing from .env")

api = PocketOption(ssid)

try:
    ok, error = api.connect()
    print("Connect result:", ok)
    print("Error:", error)

    if ok:
        print("Connected:", api.check_connect())
        print("Time synchronized:", api.is_time_synced())

        if api.is_time_synced():
            print("Server timestamp:", api.get_server_timestamp())
            print("Server datetime:", api.get_server_datetime())

finally:
    try:
        api.disconnect_websocket()
    except Exception:
        pass