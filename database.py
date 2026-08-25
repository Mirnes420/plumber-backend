import os
from sqlalchemy import create_engine, Column, String, DateTime, Text, Boolean
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import func
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL must be set in .env")

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# --- MODELS ---

class Incident(Base):
    __tablename__ = "incidents"

    id = Column(String, primary_key=True, server_default=func.gen_random_uuid())
    customer_phone = Column(String)
    plumber_phone = Column(String)
    urgency = Column(String)
    summary = Column(Text)
    raw_message = Column(Text)

    location = Column(Text)
    customer_name = Column(String)

    image_url = Column(String)
    status = Column(String, default="PENDING")
    ai_engine = Column(String)
    gear = Column(Text)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())


class Plumber(Base):
    __tablename__ = "plumbers"

    id = Column(String, primary_key=True)
    name = Column(String)
    plumber_phone = Column(String)
    dispatcher_phone = Column(String)
    active = Column(Boolean, default=True)
    password_hash = Column(Text, nullable=True)


class WhatsAppAuth(Base):
    __tablename__ = "whatsapp_auth_store"

    key = Column(String, primary_key=True)
    value = Column(Text)


class PropertyLead(Base):
    __tablename__ = "property_leads"

    id = Column(String, primary_key=True, server_default=func.gen_random_uuid())
    customer_phone = Column(String, nullable=False)
    customer_name = Column(String)
    property_id = Column(String, nullable=False)
    budget = Column(String)
    timeline = Column(String)
    language = Column(String)
    marketer_phone = Column(String, nullable=False)
    status = Column(String, default="new")
    raw_message = Column(Text)
    notification_sent = Column(Boolean, default=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())


class PropertyManager(Base):
    __tablename__ = "property_managers"

    id = Column(String, primary_key=True, server_default=func.gen_random_uuid())
    name = Column(String, nullable=False)
    phone = Column(String, nullable=False, unique=True)
    email = Column(String)


class Property(Base):
    __tablename__ = "properties"

    id = Column(String, primary_key=True)
    manager_id = Column(String)
    title = Column(String, nullable=False)
    address = Column(String, nullable=False)
    description = Column(Text)
    budget_range = Column(String)
    image_url = Column(String)
    pdf_url = Column(String)


class PropertyChatState(Base):
    __tablename__ = "property_chat_states"

    phone = Column(String, primary_key=True)
    current_property_id = Column(String)
    state = Column(String, default="awaiting_viewing")
    viewing_answer = Column(Text, nullable=True)   # stores buyer's viewing availability reply
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())



Base.metadata.create_all(bind=engine)


# --- SEED DEFAULT PLUMBERS ---

db_seed = SessionLocal()
try:
    if db_seed.query(Plumber).count() == 0:
        print("🌱 Seeding default plumbers...")
        p1 = Plumber(id="1", name="Mario Mario", plumber_phone="385919293138", dispatcher_phone="", active=True)
        p2 = Plumber(id="2", name="Luigi Mario", plumber_phone="38767103917", dispatcher_phone="", active=True)
        db_seed.add(p1)
        db_seed.add(p2)
        db_seed.commit()
        print("✅ Default plumbers seeded successfully.")
except Exception as seed_err:
    print(f"Failed to seed plumbers: {seed_err}")
finally:
    db_seed.close()


# --- INCIDENT FUNCTIONS ---

def log_incident(
    customer_phone: str,
    plumber_phone: str,
    urgency: str,
    summary: str,
    raw_message: str,
    location: str = None,
    customer_name: str = None,
    image_url: str = None,
    ai_engine: str = None,
    gear: str = None
):
    """Logs an incident using SQLAlchemy."""
    db = SessionLocal()
    try:
        new_incident = Incident(
            customer_phone=customer_phone,
            plumber_phone=plumber_phone,
            urgency=urgency,
            summary=summary,
            raw_message=raw_message,
            location=location,
            customer_name=customer_name,
            image_url=image_url,
            ai_engine=ai_engine,
            gear=gear
        )
        db.add(new_incident)
        db.commit()
        db.refresh(new_incident)
        return new_incident
    except Exception as e:
        print(f"Error logging to DB: {e}")
        db.rollback()
        return None
    finally:
        db.close()


def get_incidents():
    """Fetches all incidents using SQLAlchemy."""
    db = SessionLocal()
    try:
        incidents = db.query(Incident).order_by(Incident.timestamp.desc()).all()
        return [
            {
                "id": i.id,
                "customer_phone": i.customer_phone,
                "plumber_phone": i.plumber_phone,
                "urgency": i.urgency,
                "summary": i.summary,
                "raw_message": i.raw_message,
                "location": i.location,
                "customer_name": i.customer_name,
                "image_url": i.image_url,
                "status": i.status,
                "ai_engine": i.ai_engine,
                "timestamp": i.timestamp
            }
            for i in incidents
        ]
    except Exception as e:
        print(f"Error fetching incidents: {e}")
        return []
    finally:
        db.close()


def update_incident_status(incident_id: str, status: str):
    """Updates incident status using SQLAlchemy."""
    db = SessionLocal()
    try:
        incident = db.query(Incident).filter(Incident.id == incident_id).first()
        if incident:
            incident.status = status
            db.commit()
            return True
        return False
    except Exception as e:
        print(f"Error updating status: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def get_plumber_by_id(plumber_id: str):
    """Fetches plumber details from DB by ID."""
    if not plumber_id:
        return None
    db = SessionLocal()
    try:
        return db.query(Plumber).filter(Plumber.id == str(plumber_id), Plumber.active == True).first()
    except Exception as e:
        print(f"Error fetching plumber {plumber_id}: {e}")
        return None
    finally:
        db.close()


# --- PROPERTY LEAD FUNCTIONS ---

def log_property_lead(
    customer_phone: str,
    customer_name: str,
    property_id: str,
    budget: str,
    timeline: str,
    marketer_phone: str,
    language: str = None,
    raw_message: str = None,
    notification_sent: bool = False
):
    """Logs a property lead using SQLAlchemy."""
    db = SessionLocal()
    try:
        new_lead = PropertyLead(
            customer_phone=customer_phone,
            customer_name=customer_name,
            property_id=property_id,
            budget=budget,
            timeline=timeline,
            language=language,
            marketer_phone=marketer_phone,
            raw_message=raw_message,
            notification_sent=notification_sent
        )
        db.add(new_lead)
        db.commit()
        db.refresh(new_lead)
        return new_lead
    except Exception as e:
        print(f"Error logging property lead: {e}")
        db.rollback()
        return None
    finally:
        db.close()


def get_property_leads():
    """Fetches all property leads using SQLAlchemy."""
    db = SessionLocal()
    try:
        leads = db.query(PropertyLead).order_by(PropertyLead.timestamp.desc()).all()
        return [
            {
                "id": l.id,
                "customer_phone": l.customer_phone,
                "customer_name": l.customer_name,
                "property_id": l.property_id,
                "budget": l.budget,
                "timeline": l.timeline,
                "language": l.language,
                "marketer_phone": l.marketer_phone,
                "status": l.status,
                "notification_sent": l.notification_sent,
                "timestamp": l.timestamp
            }
            for l in leads
        ]
    except Exception as e:
        print(f"Error fetching property leads: {e}")
        return []
    finally:
        db.close()


def update_property_lead_status(lead_id: str, status: str):
    """Updates a property lead's status using SQLAlchemy."""
    db = SessionLocal()
    try:
        lead = db.query(PropertyLead).filter(PropertyLead.id == lead_id).first()
        if lead:
            lead.status = status
            db.commit()
            return True
        return False
    except Exception as e:
        print(f"Error updating lead status: {e}")
        db.rollback()
        return False
    finally:
        db.close()