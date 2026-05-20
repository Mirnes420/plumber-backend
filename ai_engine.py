import os
import sys
import json
import httpx
import asyncio
from google import genai
from google.genai import types
from dotenv import load_dotenv
import time

if sys.platform.startswith("win"):
    if hasattr(sys.stdout, "reconfigure"):
        try: sys.stdout.reconfigure(encoding="utf-8")
        except Exception: pass
    if hasattr(sys.stderr, "reconfigure"):
        try: sys.stderr.reconfigure(encoding="utf-8")
        except Exception: pass

load_dotenv()

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY must be set in .env")

client = genai.Client(api_key=GOOGLE_API_KEY)

MODEL_TIERS = [
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash"
]

SYSTEM_PROMPT = """You are an expert plumbing emergency dispatcher. 
Analyze the customer's message to determine the urgency of the plumbing issue.

URGENCY CATEGORIES:
- HIGH: Immediate danger or severe damage (e.g., flooding, burst main pipe, sewage backup inside, gas leak smell).
- MEDIUM: Significant issue but not immediate catastrophe (e.g., slow leak, broken water heater, clogged drain that isn't overflowing).
- LOW: Minor repairs or maintenance (e.g., dripping faucet, running toilet, scheduling a quote).

If an image is available, evaluate if seeing it is strictly necessary to triage this issue (e.g., an abstract description like "it looks like this" or "take a look at the picture" vs a clear text description like "my kitchen faucet is dripping" or "the pipe is leaking water" or "toilet is clogged").

OUTPUT FORMAT:
You MUST respond with a valid JSON object only:
{
    "urgency": "HIGH" | "MEDIUM" | "LOW",
    "summary": "Short 1-sentence summary of the issue",
    "action_required": true | false,
    "image_needed": true | false
}"""

OLLAMA_ENDPOINTS = []
primary_env_url = os.getenv("LOCAL_OLLAMA_API") or os.getenv("OLLAMA_CHAT_URL")
if primary_env_url:
    OLLAMA_ENDPOINTS.append(primary_env_url)

public_fallback = "https://ai.gentlemansolutions.com/api/chat"
if public_fallback not in OLLAMA_ENDPOINTS:
    OLLAMA_ENDPOINTS.append(public_fallback)

async def download_image_async(image_url: str) -> bytes:
    """Asynchronously download image bytes down a standalone thread."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(follow_redirects=True, headers=headers, timeout=10.0) as client_httpx:
            response = await client_httpx.get(image_url)
            if response.status_code == 200:
                return response.content
    except Exception as e:
        print(f"Background image download error: {e}")
    return None

async def query_ollama_non_stream(url: str, payload: dict) -> str:
    """Uses standard POST instead of stream parsing for minimum internal pipeline latency."""
    async with httpx.AsyncClient(timeout=15.0) as client_httpx:
        response = await client_httpx.post(url, json=payload)
        if response.status_code == 200:
            data = response.json()
            if "message" in data:
                return data["message"]["content"].strip()
        raise Exception(f"Ollama error: HTTP {response.status_code}")

async def analyze_triage(text: str, image_url: str = None, image_bytes: bytes = None):
    print(f"DEBUG: Starting triage analysis for text: '{text[:50]}...'")
    print("Starting the timer")
    timer_start = time.time()
    # Fire off image network down-stream instantly without blocking processing execution
    image_download_task = None
    img_data = image_bytes

    if not img_data and image_url:
        image_download_task = asyncio.create_task(download_image_async(image_url))

    ollama_success = False
    parsed_json = None
    
    for ollama_url in OLLAMA_ENDPOINTS:
        try:
            print(f"Attempting rapid text-based check with Ollama: {ollama_url}...")
            model_name = "phi3:mini"
            
            # Since we don't block for the download, we check if image parameters were requested
            has_image_attached = True if (img_data or image_url) else False
            image_presence_context = "An image was attached by the user." if has_image_attached else "No image was attached."
            user_content = f"{SYSTEM_PROMPT}\n\nContext: {image_presence_context}\nCustomer Message: {text}"
            
            payload = {
                "model": model_name,
                "messages": [{"role": "user", "content": user_content}],
                "stream": False,  # Turned off stream parsing to let Go/C++ memory complete execution instantly
                "options": {
                    "temperature": 0.0  # Setting low temperature locks fast path predictable generation
                }
            }

            raw_response = await query_ollama_non_stream(ollama_url, payload)
            
            cleaned_content = raw_response
            if "```json" in cleaned_content:
                cleaned_content = cleaned_content.split("```json")[1].split("```")[0].strip()
            elif "```" in cleaned_content:
                cleaned_content = cleaned_content.split("```")[1].split("```")[0].strip()
            
            parsed_json = json.loads(cleaned_content)
            
            if "urgency" in parsed_json and "summary" in parsed_json:
                # ROUTING INTERCEPTION
                if has_image_attached and parsed_json.get("image_needed", False):
                    print("🔄 Image confirmation true. Interrupting local route for Gemini Vision...")
                    break
                
                print("✅ Triage handled entirely via text analysis path.")
                parsed_json["ai_engine"] = f"Ollama ({model_name} @ {ollama_url})"
                ollama_success = True
                break
                
        except Exception as ollama_err:
            print(f"⚠️ Ollama endpoint bypassed or timed out: {ollama_err}")

    if ollama_success and parsed_json:
        # Cancel running task if it's not needed to clean up memory resources
        if image_download_task and not image_download_task.done():
            image_download_task.cancel()
        return parsed_json

    # Resolve background download if Gemini execution is required
    if image_download_task:
        print("Waiting for pending background image download to resolve...")
        img_data = await image_download_task

    print("🚀 Running Gemini Parallel Cluster...")
    contents = [f"Customer Message: {text}"]
    if img_data:
        image_part = types.Part.from_bytes(data=img_data, mime_type="image/jpeg")
        contents.append(image_part)

    for model_name in MODEL_TIERS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    temperature=0.0
                )
            )
            
            if response.text:
                parsed = json.loads(response.text)
                parsed["ai_engine"] = f"Gemini ({model_name})"
                return parsed
        except Exception:
            continue
    
    timer_end = time.time()
    print(f"\nTTR {timer_end - timer_start:.2f} seconds.")

    return {
        "urgency": "MEDIUM",
        "summary": "AI network timeout or fatal parsing error.",
        "action_required": True,
        "image_needed": False,
        "ai_engine": "Offline Fallback"
    }