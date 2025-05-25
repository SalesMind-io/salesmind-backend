from fastapi import FastAPI, HTTPException, Depends, BackgroundTasks
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from passlib.context import CryptContext
from datetime import datetime
import smtplib
from email.message import EmailMessage
import imaplib
import email
import openai
import threading
import time

# Konfiguration
DATABASE_URL = "sqlite:///./salesmind.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="token")
openai.api_key = "DEIN_OPENAI_KEY"

# Datenbank-Modelle
class UserDB(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    smtp_host = Column(String)
    smtp_port = Column(Integer)
    smtp_user = Column(String)
    smtp_pass = Column(String)
    email_from = Column(String)
    plan = Column(String, default="free")

class LeadDB(Base):
    __tablename__ = "leads"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    company = Column(String)
    contact_name = Column(String)
    email = Column(String)
    status = Column(String, default="new")
    score = Column(String, default="neutral")
    note = Column(Text, default="")
    followup_date = Column(DateTime, nullable=True)

Base.metadata.create_all(bind=engine)

# FastAPI-Instanz
app = FastAPI()

# Pydantic-Modelle
class User(BaseModel):
    username: str
    password: str

class SMTPSettings(BaseModel):
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    email_from: str

class Lead(BaseModel):
    company: str
    contact_name: str
    email: str
    status: str = "new"
    score: str = "neutral"
    note: str = ""
    followup_date: datetime = None

class TokenResponse(BaseModel):
    access_token: str
    token_type: str

# Authentifizierung
@app.post("/register")
def register(user: User, db: Session = Depends(SessionLocal)):
    hashed = pwd_context.hash(user.password)
    if db.query(UserDB).filter(UserDB.username == user.username).first():
        raise HTTPException(status_code=400, detail="User exists")
    db.add(UserDB(username=user.username, hashed_password=hashed))
    db.commit()
    return {"message": "Registered"}

@app.post("/token", response_model=TokenResponse)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(SessionLocal)):
    user = db.query(UserDB).filter(UserDB.username == form_data.username).first()
    if not user or not pwd_context.verify(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Wrong credentials")
    return {"access_token": user.username, "token_type": "bearer"}

# Benutzer holen
def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(SessionLocal)):
    user = db.query(UserDB).filter(UserDB.username == token).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return user

# SMTP speichern
@app.post("/smtp-settings")
def save_smtp(settings: SMTPSettings, user: UserDB = Depends(get_current_user), db: Session = Depends(SessionLocal)):
    for attr, val in settings.dict().items():
        setattr(user, attr, val)
    db.commit()
    return {"message": "SMTP gespeichert"}

# Leads abrufen/erstellen
@app.get("/leads")
def get_leads(user: UserDB = Depends(get_current_user), db: Session = Depends(SessionLocal)):
    return db.query(LeadDB).filter(LeadDB.user_id == user.id).all()

@app.post("/leads")
def create_lead(lead: Lead, user: UserDB = Depends(get_current_user), db: Session = Depends(SessionLocal)):
    db_lead = LeadDB(**lead.dict(), user_id=user.id)
    db.add(db_lead)
    db.commit()
    db.refresh(db_lead)
    return db_lead

# Mailversand
@app.post("/send-email")
def send_email(recipient: str, subject: str, body: str, user: UserDB = Depends(get_current_user)):
    try:
        msg = EmailMessage()
        msg.set_content(body)
        msg['Subject'] = subject
        msg['From'] = user.email_from
        msg['To'] = recipient

        with smtplib.SMTP(user.smtp_host, user.smtp_port) as server:
            server.starttls()
            server.login(user.smtp_user, user.smtp_pass)
            server.send_message(msg)
        return {"message": "Email gesendet"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# GPT-Antwort-Logik
def antwort_verstehen(text):
    prompt = f"Person antwortet: '{text}'. Was soll SalesMind antworten?"
    response = openai.ChatCompletion.create(
        model="gpt-3.5-turbo",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content

# IMAP-Check
def check_emails():
    while True:
        db = SessionLocal()
        users = db.query(UserDB).all()
        for user in users:
            try:
                mail = imaplib.IMAP4_SSL(user.smtp_host)
                mail.login(user.smtp_user, user.smtp_pass)
                mail.select("inbox")
                typ, data = mail.search(None, "UNSEEN")
                for num in data[0].split():
                    typ, msg_data = mail.fetch(num, '(RFC822)')
                    msg = email.message_from_bytes(msg_data[0][1])
                    from_ = msg['From']
                    body = msg.get_payload(decode=True).decode()
                    reply = antwort_verstehen(body)
                    send_email(from_, "Re:", reply, user)
                mail.logout()
            except:
                continue
        db.close()
        time.sleep(60)

threading.Thread(target=check_emails, daemon=True).start()
