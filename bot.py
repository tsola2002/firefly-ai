import os
import sys

from dotenv import load_dotenv
from iqoptionapi.stable_api import IQ_Option


# --------------------------------------------------
# Verify Python environment
# --------------------------------------------------

print("Python executable:", sys.executable)
print("Python version:", sys.version.split()[0])

# Make sure the project virtual environment is being used
if ".venv" not in sys.executable:
    print("\nWARNING: The project .venv is NOT being used.")
    print("Activate it with:")
    print("source .venv/Scripts/activate")
    sys.exit(1)


# --------------------------------------------------
# Load environment variables
# --------------------------------------------------

load_dotenv()

email = os.getenv("IQ_EMAIL")
password = os.getenv("IQ_PASSWORD")

if not email:
    print("ERROR: IQ_EMAIL is not set in your .env file.")
    sys.exit(1)

if not password:
    print("ERROR: IQ_PASSWORD is not set in your .env file.")
    sys.exit(1)


print("Email loaded:", email)
print("Password loaded:", "YES")


# --------------------------------------------------
# Connect to IQ Option
# --------------------------------------------------

print("\nConnecting to IQ Option...")

API = IQ_Option(email, password)

try:
    check, reason = API.connect()

    if check:
        print("\nSuccessfully connected!")

        # Switch to Practice Account
        API.change_balance("PRACTICE")

        balance = API.get_balance()

        print(f"Practice Balance: ${balance:.2f}")

    else:
        print("\nConnection failed:", reason)

except Exception as e:
    print("\nConnection error:")
    print(type(e).__name__, "-", str(e))

