import os
import re
import json
import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

import jwt
import requests
from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pymongo import MongoClient, ASCENDING
from pydantic import BaseModel, Field

app = FastAPI(title="AI Orb Assistant")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

MONGODB_URI = os.getenv("MONGODB_URI", "")
DB_NAME = os.getenv("MONGODB_DB_NAME", "ai_orb")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET") or secrets.token_urlsafe(48)

if MONGODB_URI:
    mongo = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=7000)
    db = mongo[DB_NAME]
    users = db.users
    messages = db.messages
    events = db.events
    users.create_index([("username", ASCENDING)], unique=True)
    messages.create_index([("user_id", ASCENDING), ("created_at", ASCENDING)])
else:
    mongo = db = users = messages = events = None


class AuthBody(BaseModel):
    username: str = Field(min_length=3, max_length=40)
    password: str = Field(min_length=6, max_length=128)


class ChatBody(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    latitude: Optional[float] = None
    longitude: Optional[float] = None


class LogoutBody(BaseModel):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def require_db():
    if users is None:
        raise HTTPException(500, "MongoDB is not configured. Set MONGODB_URI in Render.")


def hash_password(password: str, salt: Optional[bytes] = None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=64,
    )
    return base64.b64encode(salt).decode(), base64.b64encode(digest).decode()


def verify_password(password: str, salt_b64: str, digest_b64: str):
    salt = base64.b64decode(salt_b64)
    expected = base64.b64decode(digest_b64)
    actual = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=2**14,
        r=8,
        p=1,
        dklen=64,
    )
    return hmac.compare_digest(actual, expected)


def make_token(user):
    payload = {
        "sub": str(user["_id"]),
        "username": user["username"],
        "exp": datetime.now(timezone.utc) + timedelta(days=7),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def current_user(request: Request):
    require_db()
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Authentication required")
    token = auth[7:]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        from bson import ObjectId
        user = users.find_one({"_id": ObjectId(payload["sub"])})
        if not user:
            raise HTTPException(401, "User not found")
        return user
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired session")
    except Exception:
        raise HTTPException(401, "Invalid session")


def groq_headers():
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY is not configured")
    return {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }


def groq_chat(messages_payload, model="llama-3.3-70b-versatile", temperature=0.35):
    r = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers=groq_headers(),
        json={
            "model": model,
            "messages": messages_payload,
            "temperature": temperature,
        },
        timeout=60,
    )
    if not r.ok:
        raise HTTPException(502, f"Groq error: {r.text[:500]}")
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def groq_tts(text: str):
    # Current Groq Orpheus TTS supports English and Arabic.
    # For other languages the frontend can use browser speech synthesis as fallback.
    if not GROQ_API_KEY:
        return None
    clean = re.sub(r"\s+", " ", text).strip()
    if not re.search(r"[A-Za-z]", clean):
        return None
    clean = clean[:200]
    body = {
        "model": "canopylabs/orpheus-v1-english",
        "voice": "austin",
        "input": clean,
        "response_format": "wav",
    }
    r = requests.post(
        "https://api.groq.com/openai/v1/audio/speech",
        headers=groq_headers(),
        json=body,
        timeout=60,
    )
    if not r.ok:
        return None
    return base64.b64encode(r.content).decode()


def classify_message(text):
    prompt = f"""
Classify the user's request into exactly one category:
- image: the user wants an image/artwork/picture generated
- weather: the user asks about current weather, forecast-like current conditions, or weather in a place
- chat: everything else

Return ONLY valid JSON: {{"type":"image|weather|chat"}}

User message:
{text}
"""
    raw = groq_chat(
        [
            {"role": "system", "content": "You are a strict request classifier."},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
    )
    try:
        parsed = json.loads(raw)
        if parsed.get("type") in {"image", "weather", "chat"}:
            return parsed["type"]
    except Exception:
        pass
    low = text.lower()
    if any(x in low for x in ["weather", "погод", "weather in", "temperature", "температур"]):
        return "weather"
    if any(x in low for x in ["draw", "generate image", "create image", "изображен", "нарисуй", "создай картинку"]):
        return "image"
    return "chat"


def extract_weather_place(text):
    # Let Groq extract a concise place. If it cannot find one, return None.
    raw = groq_chat(
        [
            {
                "role": "system",
                "content": (
                    "Extract the requested weather place. Return ONLY JSON "
                    '{"place":"..."} where place is a city/place name, or null if '
                    "the user clearly means their current location."
                ),
            },
            {"role": "user", "content": text},
        ],
        temperature=0,
    )
    try:
        obj = json.loads(raw)
        place = obj.get("place")
        return place if isinstance(place, str) and place.strip() else None
    except Exception:
        return None


def weather_by_coords(lat, lon):
    r = requests.get(
        "https://api.openweathermap.org/data/2.5/weather",
        params={
            "lat": lat,
            "lon": lon,
            "appid": OPENWEATHER_API_KEY,
            "units": "metric",
            "lang": "en",
        },
        timeout=20,
    )
    if not r.ok:
        raise HTTPException(502, "OpenWeather could not retrieve weather.")
    return r.json()


def weather_by_place(place):
    geo = requests.get(
        "https://api.openweathermap.org/geo/1.0/direct",
        params={"q": place, "limit": 1, "appid": OPENWEATHER_API_KEY},
        timeout=20,
    )
    if not geo.ok:
        raise HTTPException(502, "OpenWeather geocoding failed.")
    places = geo.json()
    if not places:
        raise HTTPException(404, f"Could not find weather location: {place}")
    p = places[0]
    return weather_by_coords(p["lat"], p["lon"])


def weather_card(data):
    weather = data.get("weather", [{}])[0]
    main = data.get("main", {})
    wind = data.get("wind", {})
    return {
        "city": data.get("name", "Unknown"),
        "country": data.get("sys", {}).get("country", ""),
        "description": weather.get("description", "").capitalize(),
        "icon": weather.get("icon", "01d"),
        "temperature": round(main.get("temp", 0)),
        "feels_like": round(main.get("feels_like", 0)),
        "humidity": main.get("humidity"),
        "pressure": main.get("pressure"),
        "wind": round(wind.get("speed", 0), 1),
        "units": "°C",
    }


def weather_spoken(card):
    return (
        f"Current weather in {card['city']}: {card['description']}. "
        f"The temperature is {card['temperature']} degrees Celsius, "
        f"feels like {card['feels_like']}, with humidity at {card['humidity']} percent."
    )


def image_generate(english_prompt):
    if not CLOUDFLARE_ACCOUNT_ID or not CLOUDFLARE_API_TOKEN:
        raise HTTPException(500, "Cloudflare credentials are not configured.")
    url = (
        f"https://api.cloudflare.com/client/v4/accounts/"
        f"{CLOUDFLARE_ACCOUNT_ID}/ai/run/@cf/black-forest-labs/flux-1-schnell"
    )
    r = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"prompt": english_prompt, "steps": 4},
        timeout=120,
    )
    if not r.ok:
        raise HTTPException(502, f"Cloudflare image error: {r.text[:500]}")
    result = r.json().get("result", {})
    image_b64 = result.get("image")
    if not image_b64:
        raise HTTPException(502, "Cloudflare returned no image.")
    return image_b64


def save_message(user_id, role, content, kind="chat", metadata=None):
    messages.insert_one({
        "user_id": user_id,
        "role": role,
        "content": content,
        "kind": kind,
        "metadata": metadata or {},
        "created_at": utcnow(),
    })


@app.get("/")
def home():
    return FileResponse("index.html")


@app.get("/style.css")
def css():
    return FileResponse("style.css", media_type="text/css")


@app.get("/app.js")
def js():
    return FileResponse("app.js", media_type="application/javascript")


@app.post("/api/register")
def register(body: AuthBody):
    require_db()
    username = body.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,40}", username):
        raise HTTPException(400, "Username: 3–40 characters, letters/numbers/._- only.")
    if users.find_one({"username": username}):
        raise HTTPException(409, "This username is already registered.")
    salt, digest = hash_password(body.password)
    doc = {
        "username": username,
        "password_salt": salt,
        "password_hash": digest,
        "created_at": utcnow(),
        "last_login": utcnow(),
        "last_logout": None,
        "location": None,
    }
    result = users.insert_one(doc)
    user = users.find_one({"_id": result.inserted_id})
    events.insert_one({"user_id": str(result.inserted_id), "type": "register", "created_at": utcnow()})
    return {"token": make_token(user), "username": username, "new_user": True}


@app.post("/api/login")
def login(body: AuthBody):
    require_db()
    username = body.username.strip()
    user = users.find_one({"username": username})
    if not user or not verify_password(body.password, user["password_salt"], user["password_hash"]):
        raise HTTPException(401, "Incorrect username or password.")
    users.update_one({"_id": user["_id"]}, {"$set": {"last_login": utcnow()}})
    events.insert_one({"user_id": str(user["_id"]), "type": "login", "created_at": utcnow()})
    return {"token": make_token(user), "username": username, "new_user": False}


@app.post("/api/logout")
def logout(body: LogoutBody, user=Depends(current_user)):
    users.update_one({"_id": user["_id"]}, {"$set": {"last_logout": utcnow()}})
    events.insert_one({"user_id": str(user["_id"]), "type": "logout", "created_at": utcnow()})
    return {"ok": True}


@app.post("/api/location")
async def save_location(request: Request, user=Depends(current_user)):
    data = await request.json()
    lat = data.get("latitude")
    lon = data.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        raise HTTPException(400, "Invalid coordinates.")
    users.update_one(
        {"_id": user["_id"]},
        {"$set": {"location": {"latitude": lat, "longitude": lon, "updated_at": utcnow()}}},
    )
    return {"ok": True}


@app.get("/api/history")
def history(user=Depends(current_user)):
    rows = list(
        messages.find({"user_id": str(user["_id"])}).sort("created_at", ASCENDING).limit(100)
    )
    out = []
    for m in rows:
        out.append({
            "role": m["role"],
            "content": m["content"],
            "kind": m.get("kind", "chat"),
            "metadata": m.get("metadata", {}),
            "created_at": m["created_at"].isoformat(),
        })
    return {"messages": out}


@app.post("/api/transcribe")
async def transcribe(audio: UploadFile = File(...), user=Depends(current_user)):
    if not GROQ_API_KEY:
        raise HTTPException(500, "GROQ_API_KEY is not configured.")
    raw = await audio.read()
    if len(raw) > 15 * 1024 * 1024:
        raise HTTPException(413, "Audio file is too large.")
    files = {
        "file": (
            audio.filename or "recording.webm",
            raw,
            audio.content_type or "audio/webm",
        )
    }
    data = {
        "model": "whisper-large-v3-turbo",
        "response_format": "json",
        "temperature": "0",
    }
    r = requests.post(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        files=files,
        data=data,
        timeout=90,
    )
    if not r.ok:
        raise HTTPException(502, f"Groq transcription error: {r.text[:500]}")
    return {"text": r.json().get("text", "").strip()}


@app.post("/api/chat")
def chat(body: ChatBody, user=Depends(current_user)):
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Empty message.")

    user_id = str(user["_id"])
    kind = classify_message(text)
    save_message(user_id, "user", text, kind)

    if kind == "image":
        translation = groq_chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Translate the image request into a detailed English image-generation "
                        "prompt. Preserve every important visual detail. Return ONLY the prompt."
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.2,
        )
        image_b64 = image_generate(translation)
        description = groq_chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Write a concise, vivid description of the image that was generated "
                        "from the following prompt. This text will be spoken aloud. Do not "
                        "claim to see details that are not implied by the prompt."
                    ),
                },
                {"role": "user", "content": translation},
            ],
            temperature=0.5,
        )
        audio = groq_tts(description)
        metadata = {
            "image": f"data:image/jpeg;base64,{image_b64}",
            "prompt": translation,
            "audio": audio,
        }
        save_message(user_id, "assistant", description, "image", metadata)
        return {
            "type": "image",
            "text": description,
            "image": metadata["image"],
            "audio": audio,
        }

    if kind == "weather":
        if not OPENWEATHER_API_KEY:
            raise HTTPException(500, "OPENWEATHER_API_KEY is not configured.")
        place = extract_weather_place(text)
        if place:
            data = weather_by_place(place)
        else:
            lat = body.latitude
            lon = body.longitude
            if lat is None or lon is None:
                loc = user.get("location") or {}
                lat = loc.get("latitude")
                lon = loc.get("longitude")
            if lat is None or lon is None:
                raise HTTPException(
                    400,
                    "Please allow location access or specify a city for the weather request.",
                )
            data = weather_by_coords(lat, lon)
        card = weather_card(data)
        spoken = weather_spoken(card)
        audio = groq_tts(spoken)
        save_message(user_id, "assistant", spoken, "weather", {"weather": card, "audio": audio})
        return {"type": "weather", "text": spoken, "weather": card, "audio": audio}

    context_rows = list(
        messages.find({"user_id": user_id}).sort("created_at", -1).limit(12)
    )
    context_rows.reverse()
    convo = [
        {
            "role": "system",
            "content": (
                "You are a friendly, concise AI assistant inside a realistic glowing orb. "
                "Answer naturally in the same language as the user. Do not use markdown tables. "
                "For normal conversation only; image and weather requests are routed separately."
            ),
        }
    ]
    for m in context_rows:
        convo.append({"role": m["role"], "content": m["content"]})
    answer = groq_chat(convo, temperature=0.45)
    audio = groq_tts(answer)
    save_message(user_id, "assistant", answer, "chat", {"audio": audio})
    return {"type": "chat", "text": answer, "audio": audio}


@app.on_event("startup")
def startup():
    if db is not None:
        try:
            db.command("ping")
            print("MongoDB connected")
        except Exception as exc:
            print("MongoDB connection warning:", exc)
