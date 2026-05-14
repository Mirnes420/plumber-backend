import streamlit as st
import pandas as pd
from database import get_incidents, update_incident_status, log_incident
import os
from dotenv import load_dotenv
import asyncio
from ai_engine import analyze_triage
import multiprocessing
import uvicorn
from main import app

load_dotenv()

# Function to run FastAPI
def run_fastapi():
    uvicorn.run(app, host="0.0.0.0", port=8000)

# Start FastAPI in a separate process if not already running
# We use a global check to avoid starting it multiple times on every rerun
if "fastapi_started" not in st.session_state:
    try:
        # We use a simple port check or just try starting it
        p = multiprocessing.Process(target=run_fastapi, daemon=True)
        p.start()
        st.session_state.fastapi_started = True
    except Exception as e:
        print(f"FastAPI start error: {e}")

st.set_page_config(page_title="Plumbing Triage Admin", page_icon="🚰", layout="wide")

# Simple Authentication
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

def check_password():
    """Returns True if the user had the correct password."""
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False
    
    if st.session_state["authenticated"]:
        return True

    with st.form("Login"):
        password = st.text_input("Admin Password", type="password")
        submit = st.form_submit_button("Login")
        if submit:
            if password == ADMIN_PASSWORD:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Invalid password")
    return False

if check_password():
    st.title("🚰 Plumbing Emergency Dashboard")
    
    # Initialize session state for AI Simulator results
    if 'sim_result' not in st.session_state:
        st.session_state.sim_result = None
        st.session_state.sim_data = {}

    tab1, tab2 = st.tabs(["📊 Incident Log", "🧪 AI Simulator"])

    with tab1:
        st.markdown("Monitor and manage incoming WhatsApp triage requests.")
        incidents = get_incidents()
        
        if not incidents:
            st.info("No incidents logged yet.")
        else:
            df = pd.DataFrame(incidents)
            
            st.sidebar.header("Filters")
            urgency_filter = st.sidebar.multiselect("Urgency", options=["HIGH", "MEDIUM", "LOW"], default=["HIGH", "MEDIUM", "LOW"])
            status_filter = st.sidebar.multiselect("Status", options=["PENDING", "RESOLVED"], default=["PENDING", "RESOLVED"])
            
            filtered_df = df[df['urgency'].isin(urgency_filter) & df['status'].isin(status_filter)]

            col1, col2, col3 = st.columns(3)
            col1.metric("Total Incidents", len(df))
            col2.metric("High Urgency", len(df[df['urgency'] == "HIGH"]))
            col3.metric("Pending", len(df[df['status'] == "PENDING"]))

            st.subheader("Recent Incidents")
            
            for idx, row in filtered_df.iterrows():
                with st.expander(f"[{row['urgency']}] {row['summary']} - {row['customer_phone']}"):
                    c1, c2 = st.columns([2, 1])
                    with c1:
                        st.write(f"**Customer:** {row['customer_phone']}")
                        st.write(f"**Time:** {row['timestamp']}")
                        st.write(f"**Message:** {row['raw_message']}")
                        if row['image_url']:
                            st.image(row['image_url'], caption="Attached Media", width=300)
                    
                    with c2:
                        st.write(f"**Current Status:** {row['status']}")
                        if row['status'] == "PENDING":
                            if st.button("Mark as RESOLVED", key=f"res_{row['id']}"):
                                update_incident_status(row['id'], "RESOLVED")
                                st.success("Updated!")
                                st.rerun()
                        else:
                            if st.button("Mark as PENDING", key=f"pen_{row['id']}"):
                                update_incident_status(row['id'], "PENDING")
                                st.success("Updated!")
                                st.rerun()

            if st.checkbox("Show Raw Data"):
                st.dataframe(filtered_df)

    with tab2:
        st.header("🧪 AI Triage Simulator")
        st.write("Test the AI analysis logic by simulating a message from a customer.")
        
        with st.form("simulator_form"):
            sim_phone = st.text_input("Customer Phone (Simulated)", value="+123456789")
            sim_msg = st.text_area("Customer Message", placeholder="e.g. Help! My kitchen is flooding from a burst pipe!")
            sim_image = st.text_input("Image URL (Optional)", placeholder="https://example.com/leak.jpg")
            
            submitted = st.form_submit_button("Run Triage Analysis")
            
            if submitted:
                if not sim_msg:
                    st.error("Please enter a message.")
                else:
                    with st.spinner("AI is analyzing..."):
                        # Execute shared logic (AI + DB + Notification)
                        from shared_logic import process_incoming_incident
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        result, notified = loop.run_until_complete(
                            process_incoming_incident(sim_phone, sim_msg, sim_image)
                        )
                        
                        # Save to session state to persist after form submission
                        st.session_state.sim_result = result
                        st.session_state.sim_notified = notified
                        st.session_state.sim_data = {
                            "phone": sim_phone,
                            "msg": sim_msg,
                            "img": sim_image
                        }
                        st.success("Analysis complete and logged to database!")

        # Display results outside the form
        if st.session_state.sim_result:
            st.divider()
            st.subheader("Analysis Result")
            st.json(st.session_state.sim_result)
            
            if st.session_state.sim_notified:
                st.warning("🚨 EMERGENCY: A notification has been sent to the Plumber via WhatsApp!")
            else:
                st.info("Status: Logged and processed. No emergency alert sent.")
            
            if st.button("Clear Simulation Result", use_container_width=True):
                st.session_state.sim_result = None
                st.session_state.sim_notified = False
                st.rerun()

    if st.button("Refresh Dashboard"):
        st.rerun()