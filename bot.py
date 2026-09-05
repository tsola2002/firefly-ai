import os
from iqoptionapi.stable_api import IQ_Option

email = os.getenv("IQ_EMAIL")
password = os.getenv("IQ_PASSWORD")

API = IQ_Option(email, password)

check, reason = API.connect()

if check:
    print("Successfully connected!")
    API.change_balance("PRACTICE")
    print(f"Practice Balance: ${API.get_balance():.2f}")
else:
    print(f"Connection failed: {reason}")