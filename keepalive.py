import asyncio
import httpx
import sys

APP_URL = "https://plumber-emergency.gentlemansolutions.com"
BACKEND_URL = "https://plumber-backend-fnh6.onrender.com"

async def keep_alive_daemon(url: str, label: str):
    """
    Background daemon that pings a target endpoint every 10 minutes
    to prevent Render containers from dropping into a cold start state.
    """
    print(f"🚀 Keep-alive daemon for [{label}] successfully initialized.")
    
    async with httpx.AsyncClient(timeout=10.0) as client:
        while True:
            try:
                response = await client.get(url)
                print(f"📡 [{label}] Keep-alive ping sent to {url}. Status: {response.status_code}")
            except Exception as e:
                print(f"⚠️ [{label}] Keep-alive daemon network ping missed: {e}")
            
            # Sleep for 10 minutes (600 seconds) before sending the next heartbeat
            await asyncio.sleep(600)

async def main():
    # Run both tracking tasks concurrently under a single execution frame
    await asyncio.gather(
        keep_alive_daemon(APP_URL, "Frontend Portal"),
        keep_alive_daemon(BACKEND_URL, "Triage Backend")
    )

if __name__ == "__main__":
    try:
        # Cleanly boots and handles the asynchronous event loop Lifecycle
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Keep-alive daemon cluster stopped cleanly via user interrupt.")
        sys.exit(0)