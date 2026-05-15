import os
import json
import httpx
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

# Configure Gemini
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
if not GOOGLE_API_KEY:
    raise ValueError("GOOGLE_API_KEY must be set in .env")

client = genai.Client(api_key=GOOGLE_API_KEY)

# THE REQUIRED HIERARCHY
MODEL_TIERS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash"
]

SYSTEM_PROMPT = """
You are an expert plumbing emergency dispatcher. 
Analyze the customer's message and any attached images to determine the urgency of the plumbing issue.

URGENCY CATEGORIES:
- HIGH: Immediate danger or severe damage (e.g., flooding, burst main pipe, sewage backup inside, gas leak smell).
- MEDIUM: Significant issue but not immediate catastrophe (e.g., slow leak, broken water heater, clogged drain that isn't overflowing).
- LOW: Minor repairs or maintenance (e.g., dripping faucet, running toilet, scheduling a quote).

OUTPUT FORMAT:
You MUST respond with a valid JSON object only:
{
    "urgency": "HIGH" | "MEDIUM" | "LOW",
    "summary": "Short 1-sentence summary of the issue",
    "action_required": true | false
}
"""

async def analyze_triage(text: str, image_url: str = None, image_bytes: bytes = None):
    """
    Analyzes text and optionally an image using a tiered fallback system.
    """
    contents = [f"Customer Message: {text}"]
    
    # 1. Handle image (either bytes or URL)
    if image_bytes:
        image_part = types.Part.from_bytes(
            data=image_bytes,
            mime_type="image/jpeg"
        )
        contents.append(image_part)
    elif image_url:
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
            async with httpx.AsyncClient(follow_redirects=True, headers=headers) as client_httpx:
                response = await client_httpx.get(image_url)
                if response.status_code == 200:
                    image_part = types.Part.from_bytes(
                        data=response.content,
                        mime_type="image/jpeg"
                    )
                    contents.append(image_part)
                else:
                    print(f"Failed to download image: HTTP {response.status_code}")
        except Exception as e:
            print(f"Image processing error: {e}")

    # 2. Sequential Fallback Logic
    for model_name in MODEL_TIERS:
        try:
            print(f"Attempting analysis with {model_name}...")
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json"
                )
            )
            
            if response.text:
                return json.loads(response.text)
        
        except Exception as e:
            print(f"Model {model_name} failed: {e}. Moving to next tier...")
            continue  # Hit the next model in the list

    # 3. Final Fallback if ALL models crash
    return {
        "urgency": "MEDIUM",
        "summary": "Critical AI Failure. All model tiers (2.5 & 2.0) failed. Manual review required.",
        "action_required": True
    }