import os
import requests
from dotenv import load_dotenv

load_dotenv()  # reads EMAILER_API_KEY from the .env file next to this script

response = requests.post(
    "https://x3z4edjwve.execute-api.us-east-1.amazonaws.com/v1/send",
    headers={"x-api-key": os.environ["EMAILER_API_KEY"]},
    json={
        "from_email": "no-reply@brandflyers.com",
        "template_id": "order-confirmation",
        "template_params": {
            "to_email": "akhileshss991@gmail.com",
            "order_number": "BF-10882",
            "delivery_date": "28 Sep 2026",
            "order_url": "https://brandflyers.com/orders/1042",
            "to_name": "Akhilesh",
            "product": "A5 flyers, 500 copies",
            "quantity": "500",
            "total": "Rs 4,500"
        },
    },
    timeout=10,
)
print(response.status_code, response.json())
