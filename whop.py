import math
import httpx

from dotenv import load_dotenv
load_dotenv()

# Whop API Config
WHOP_API_KEY = os.getenv("WHOP_API_KEY")
WHOP_HEADERS = {
    "Authorization": f"Bearer {WHOP_API_KEY}",
    "Content-Type": "application/json"
}

PLAN_LIMITS = {
    "starter": {"included_min": 100, "rate": 0.15},
    "pro": {"included_min": 300, "rate": 0.12},
    "business": {"included_min": 1000, "rate": 0.10},
}

async def process_monthly_overages():
    # Fetch subscriptions due for cycle reset
    subscriptions = await db.fetch("SELECT * FROM user_subscriptions WHERE cycle_start_date <= NOW() - INTERVAL '30 days'")

    async with httpx.AsyncClient() as client:
        for sub in subscriptions:
            plan_info = PLAN_LIMITS.get(sub["plan_tier"].lower(), PLAN_LIMITS["starter"])
            
            # Convert seconds to rounded-up minutes
            used_minutes = math.ceil(sub["used_seconds"] / 60)
            included_minutes = plan_info["included_min"]
            rate = plan_info["rate"]

            if used_minutes > included_minutes:
                extra_minutes = used_minutes - included_minutes
                charge_amount = round(extra_minutes * rate, 2)

                # Charge via Whop API
                payload = {
                    "member_id": sub["whop_member_id"],
                    "payment_method_id": sub["whop_payment_method_id"],
                    "amount": charge_amount,
                    "currency": "usd",
                    "description": f"Overage fee: {extra_minutes} extra minutes used."
                }

                response = await client.post(
                    "https://api.whop.com/v5/payments",
                    headers=WHOP_HEADERS,
                    json=payload
                )

                if response.status_code in (200, 201):
                    print(f"✅ Successfully charged ${charge_amount} to {sub['plumber_id']}")
                else:
                    print(f"❌ Failed to charge overage: {response.text}")

            # Reset cycle and counter for next 30 days
            await db.execute(
                "UPDATE user_subscriptions SET used_seconds = 0, cycle_start_date = NOW() WHERE id = $1",
                sub["id"]
            )