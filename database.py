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

# Handle the case where SQLAlchemy might need 'postgresql://' instead of 'postgres://'
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Incident(Base):
    __tablename__ = "incidents"

    id = Column(String, primary_key=True, server_default=func.gen_random_uuid())
    customer_phone = Column(String)
    plumber_phone = Column(String)
    urgency = Column(String)
    summary = Column(Text)
    raw_message = Column(Text)
    image_url = Column(String)
    status = Column(String, default="PENDING")
    timestamp = Column(DateTime(timezone=True), server_default=func.now())

# Create table if it doesn't exist
Base.metadata.create_all(bind=engine)

def log_incident(customer_phone: str, plumber_phone: str, urgency: str, summary: str, raw_message: str, image_url: str = None):
    """Logs an incident using SQLAlchemy."""
    db = SessionLocal()
    try:
        new_incident = Incident(
            customer_phone=customer_phone,
            plumber_phone=plumber_phone,
            urgency=urgency,
            summary=summary,
            raw_message=raw_message,
            image_url=image_url
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
        # Convert to list of dicts for compatibility with the rest of the app
        return [
            {
                "id": i.id,
                "customer_phone": i.customer_phone,
                "plumber_phone": i.plumber_phone,
                "urgency": i.urgency,
                "summary": i.summary,
                "raw_message": i.raw_message,
                "image_url": i.image_url,
                "status": i.status,
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
