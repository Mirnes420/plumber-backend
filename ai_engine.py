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

async def analyze_triage(text: str, image_url: str = None):
    """
    Analyzes text and optionally an image using google-genai (Gemini 1.5 Pro).
    Returns a dict with urgency, summary, and action_required.
    """
    try:
        contents = [f"Customer Message: {text}"]
        
        if image_url:
            # Set a User-Agent to avoid being blocked by some CDNs (like Twilio's)
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
        
        # Generate content with system instruction
        response = client.models.generate_content(
            model='gemini-2.5-flash-lite',
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json"
            )
        )
        
        # Parse JSON response
        result = json.loads(response.text)
        return result
    
    except Exception as e:
        print(f"AI Engine Error: {e}")
        # Default fallback as per requirements
        return {
            "urgency": "MEDIUM",
            "summary": f"AI Analysis Failed. Manual review required. {e}",
            "action_required": True
        }
