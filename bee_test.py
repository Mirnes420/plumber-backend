import os
import dotenv
import requests  # axios の代わりに requests を使う

dotenv.load_dotenv()

device = os.getenv("TEXTBEE_DEVICE_ID")
api = os.getenv("TEXTBEE_API")

def test_bee():
    print(f"Device ID: {device}")
    print(f"API Key: {api}")

    url = f"https://api.textbee.dev/api/v1/gateway/devices/{device}/send-sms"
    data = {
        "recipients": ["+387671034917"],
        "message": "Hello World!",
    }
    headers = {"x-api-key": api}

    response = requests.post(url, json=data, headers=headers)
    print(f"Status: {response.status_code}")
    print(f"Response: {response.text}")

    if response.status_code == 200:
        print("SMS sent successfully!")
    else:
        print("Failed to send SMS")

test_bee()