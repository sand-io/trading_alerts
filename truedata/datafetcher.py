from truedata_ws.websocket.TD import TD
import time
import os
from pathlib import Path
from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
load_dotenv(ENV_FILE)

USERNAME = os.getenv("USERNAME")
PASSWORD = os.getenv("PASSWORD")

# Port 8084 is your assigned port
td_obj = TD(USERNAME, PASSWORD, live_port=8084, url='push.truedata.in')

# Subscribe to symbols (use correct TrueData symbol format)
symbols = ['HINDPETRO-I']  # Example symbols; replace with actual ones
req_ids = td_obj.start_live_data(symbols)

time.sleep(2)  # Allow touchline data to populate

last_price = {}

while True:
    time.sleep(0.5)
    for req_id in req_ids:
        data=td_obj.live_data.get(req_id)
        # print(data)

        if last_price.get(req_id) != data.ltp:
            print(data.symbol, data.ltp)
            last_price[req_id] = data.ltp
