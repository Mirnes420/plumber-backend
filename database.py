import os
from datetime import datetime, timedelta, timezone
from sqlalchemy import create_engine, Column, String, DateTime, Text, Boolean, Integer
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
    contractor_phone = Column(String)
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


class Contractor(Base):
    __tablename__ = "contractors"

    id = Column(String, primary_key=True)
    name = Column(String)
    contractor_phone = Column(String)
    dispatcher_phone = Column(String)
    active = Column(Boolean, default=True)
    password_hash = Column(Text, nullable=True)
    email = Column(String(255), nullable=True)
    language = Column(String, default="English")


class WhatsAppAuth(Base):
    __tablename__ = "whatsapp_auth_store"

    key = Column(String, primary_key=True)
    value = Column(Text)

# ── Subscription / Usage Tracking ──────────────────────────────────────────────
class Subscription(Base):
    """Tracks per-contractor plan tier, minute usage, and billing cycle."""
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contractor_id = Column(String, nullable=False, unique=True, index=True)
    plan_tier = Column(String, nullable=False, default="starter")  # starter | pro | business
    used_seconds = Column(Integer, nullable=False, default=0)
    whop_member_id = Column(String, nullable=True)
    whop_payment_method_id = Column(String, nullable=True)
    cycle_start_date = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


Base.metadata.create_all(bind=engine)


# ── Usage Helpers ───────────────────────────────────────────────────────────────

def increment_usage_seconds(contractor_id: str, seconds: int) -> bool:
    """Atomically adds *seconds* to the subscription row for *contractor_id*."""
    if seconds <= 0:
        return True
    db = SessionLocal()
    try:
        sub = db.query(Subscription).filter(Subscription.contractor_id == contractor_id).first()
        if sub is None:
            # Auto-create a starter subscription row so tracking never silently drops
            sub = Subscription(contractor_id=contractor_id, plan_tier="starter")
            db.add(sub)
            db.flush()
        sub.used_seconds += int(seconds)
        db.commit()
        return True
    except Exception as e:
        print(f"[USAGE] increment_usage_seconds error for {contractor_id}: {e}")
        db.rollback()
        return False
    finally:
        db.close()


def get_subscriptions_due_for_billing():
    """Returns all Subscription rows whose billing cycle has passed 30 days."""
    db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        return (
            db.query(Subscription)
            .filter(Subscription.cycle_start_date <= cutoff)
            .all()
        )
    except Exception as e:
        print(f"[BILLING] Error fetching subscriptions due: {e}")
        return []
    finally:
        db.close()


def reset_subscription_cycle(subscription_id: int):
    """Zeroes out used_seconds and advances cycle_start_date to now."""
    db = SessionLocal()
    try:
        sub = db.query(Subscription).filter(Subscription.id == subscription_id).first()
        if sub:
            sub.used_seconds = 0
            sub.cycle_start_date = datetime.now(timezone.utc)
            db.commit()
    except Exception as e:
        print(f"[BILLING] Error resetting cycle for id={subscription_id}: {e}")
        db.rollback()
    finally:
        db.close()


# --- SEED DEFAULT CONTRACTORS ---

db_seed = SessionLocal()
try:
    if db_seed.query(Contractor).count() == 0:
        print("🌱 Seeding default contractors...")
        p1 = Contractor(id="1", name="Mario Mario", contractor_phone="385919293138", dispatcher_phone="", active=True)
        p2 = Contractor(id="2", name="Luigi Mario", contractor_phone="38767103917", dispatcher_phone="", active=True)
        db_seed.add(p1)
        db_seed.add(p2)
        db_seed.commit()
        print("✅ Default contractors seeded successfully.")
except Exception as seed_err:
    print(f"Failed to seed contractors: {seed_err}")
finally:
    db_seed.close()


# --- INCIDENT FUNCTIONS ---

def log_incident(
    customer_phone: str,
    contractor_phone: str,
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
            contractor_phone=contractor_phone,
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
                "contractor_phone": i.contractor_phone,
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


def get_contractor_by_id(contractor_id: str):
    """Fetches contractor details from DB by ID."""
    if not contractor_id:
        return None
    db = SessionLocal()
    try:
        return db.query(Contractor).filter(Contractor.id == str(contractor_id), Contractor.active == True).first()
    except Exception as e:
        print(f"Error fetching contractor {contractor_id}: {e}")
        return None
    finally:
        db.close()
