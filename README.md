# WhatsApp Emergency Triage Bot 🚰

A production-ready AI-powered triage system for plumbing businesses. It uses Google Gemini 1.5 Pro to analyze incoming WhatsApp messages (text + images) and classify them by urgency.

## Features
- **AI Triage**: Automatically determines if a request is HIGH, MEDIUM, or LOW urgency.
- **Emergency Alerts**: Instantly notifies the plumber via WhatsApp/SMS for HIGH urgency incidents.
- **Auto-Reply**: Sends immediate confirmation to customers.
- **Admin Dashboard**: View and manage incidents via a Streamlit interface.
- **Data Persistence**: All interactions logged in Supabase.

## Tech Stack
- **Backend**: FastAPI (Python 3.11)
- **AI**: Google Gemini 1.5 Pro
- **WhatsApp**: Twilio Messaging API
- **Database**: Supabase (PostgreSQL)
- **Frontend**: Streamlit

## Setup Instructions

### 1. Prerequisites
- Python 3.11+
- Twilio Account (for WhatsApp Sandbox/API)
- Google AI Studio API Key (for Gemini)
- Supabase Project

### 2. Environment Variables
Copy `.env.example` to `.env` and fill in your credentials:
```bash
cp .env.example .env
```

### 3. Supabase Database Setup
Run the following SQL in your Supabase SQL Editor to create the `incidents` table:
```sql
CREATE TABLE incidents (
    id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
    customer_phone TEXT,
    plumber_phone TEXT,
    urgency TEXT, -- 'HIGH', 'MEDIUM', 'LOW'
    summary TEXT,
    raw_message TEXT,
    image_url TEXT,
    status TEXT DEFAULT 'PENDING',
    timestamp TIMESTAMPTZ DEFAULT now()
);
```

### 4. Local Installation
```bash
pip install -r requirements.txt
```

### 5. Running the Application
**Start the API (Webhook Handler):**
```bash
uvicorn main:app --reload
```

**Start the Admin Dashboard:**
```bash
streamlit run streamlit_app.py
```

### 6. Twilio Configuration
1. Go to the Twilio Console.
2. Set your WhatsApp Sandbox Webhook URL to: `https://your-domain.com/webhook` (ensure it's a POST request).
3. If testing locally, use **ngrok**: `ngrok http 8000` and use the ngrok URL.

## Deployment
This project is dockerized. You can deploy it to **Railway**, **Render**, or **DigitalOcean**.
The `Dockerfile` defaults to running the FastAPI backend. To run the dashboard, you may need a separate service or a process manager.
