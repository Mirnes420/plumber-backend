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

# Authentication
if 'logged_in_user' not in st.session_state:
    st.title("🚰 Plumbing Emergency Dashboard")
    st.subheader("🔐 Login")
    with st.container():
        user_id_input = st.text_input("Enter your ID (Plumber ID or Admin ID)", placeholder="e.g. 1 or admin")
        if st.button("Access Dashboard", use_container_width=True):
            # Admin Check
            if user_id_input.lower() == "admin":
                st.session_state.logged_in_user = {
                    "id": "admin",
                    "name": "Super Admin",
                    "role": "admin"
                }
                st.success("Welcome, Admin!")
                st.rerun()
            
            # Plumber Check
            from database import get_plumber_by_id
            plumber = get_plumber_by_id(user_id_input)
            if plumber:
                st.session_state.logged_in_user = {
                    "id": plumber.id,
                    "name": plumber.name,
                    "phone": plumber.plumber_phone,
                    "role": "plumber"
                }
                st.success(f"Welcome, {plumber.name}!")
                st.rerun()
            else:
                st.error("Invalid ID. Please try again.")
    st.stop()

# If logged in, show the dashboard
user_info = st.session_state.logged_in_user
is_admin = user_info['role'] == "admin"

st.title("🚰 Plumbing Emergency Dashboard")
st.sidebar.success(f"👤 Logged in as: {user_info['name']} ({user_info['role'].upper()})")
if st.sidebar.button("Logout"):
    del st.session_state.logged_in_user
    st.rerun()
    
# Initialize session state for AI Simulator results
if 'sim_result' not in st.session_state:
    st.session_state.sim_result = None
    st.session_state.sim_data = {}

# Tabs - Plumbers only get the Log, Admins get both
tab_names = ["📊 Incident Log"]
if is_admin:
    tab_names.append("🧪 AI Simulator")

tabs = st.tabs(tab_names)
tab1 = tabs[0]
if is_admin:
    tab2 = tabs[1]

with tab1:
    st.markdown(f"Monitor and manage WhatsApp triage requests for {'everyone' if is_admin else 'your service'}.")
    
    # URL-based filtering logic
    query_params = st.query_params
    default_urgency = query_params.get("urgency", "HIGH") # Default to HIGH as requested
    plumber_number_override = query_params.get("plumber_number")
    if isinstance(default_urgency, list): default_urgency = default_urgency[0]
    if isinstance(plumber_number_override, list): plumber_number_override = plumber_number_override[0]
    
    # Fetch data
    all_incidents = get_incidents()
    
    # Filtering based on role
    if is_admin:
        incidents = all_incidents
    else:
        incidents = [i for i in all_incidents if i['plumber_phone'] == user_info['phone']]
    
    if not incidents:
        st.info("No incidents logged yet.")
    else:
        df = pd.DataFrame(incidents)
        
        st.sidebar.header("Filters")
        
        # Select filters based on query params or default
        available_urgencies = ["HIGH", "MEDIUM", "LOW"]
        selected_urgencies = st.sidebar.multiselect(
            "Urgency", 
            options=available_urgencies, 
            default=[default_urgency] if default_urgency in available_urgencies else available_urgencies
        )
        
        status_filter = st.sidebar.multiselect("Status", options=["PENDING", "RESOLVED"], default=["PENDING", "RESOLVED"])
        
        filtered_df = df[df['urgency'].isin(selected_urgencies) & df['status'].isin(status_filter)]

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
                    if pd.notna(row['image_url']) and isinstance(row['image_url'], str) and row['image_url'].strip():
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

if is_admin:
    with tab2:
        st.header("🧪 AI Triage Simulator")
        st.write("Test the AI analysis logic by simulating a message from a customer.")
        
        with st.form("simulator_form"):
            sim_phone = st.text_input("Customer Phone (Simulated)", value="+123456789")
            sim_msg = st.text_area("Customer Message", placeholder="e.g. Help! My kitchen is flooding from a burst pipe!")
            sim_image = st.text_input("Image URL (Optional)", placeholder="https://example.com/leak.jpg")
            
            # Allow admin to pick which plumber to send to
            from database import SessionLocal, Plumber
            db = SessionLocal()
            all_plumbers = db.query(Plumber).all()
            db.close()
            
            plumber_options = {f"{p.name} ({p.id})": p.id for p in all_plumbers}
            selected_plumber_name = st.selectbox("Route To Plumber", options=list(plumber_options.keys()))
            sim_plumber_id = plumber_options[selected_plumber_name]
            
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
                            process_incoming_incident(
                                sim_phone, 
                                sim_msg, 
                                sim_image, 
                                plumber_override=sim_plumber_id
                            )
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
        