import os
import base64
import json
import sqlite3
import hashlib
import secrets
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Depends, Request
from fastapi.responses import FileResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# Load environment variables
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DB_FILE = BASE_DIR / "ecorota.db"
INDEX_FILE = BASE_DIR / "index.html"

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

USE_SUPABASE = bool(
    SUPABASE_URL
    and SUPABASE_SERVICE_ROLE_KEY
    and "SEU-PROJETO" not in SUPABASE_URL
    and "SUA_CHAVE" not in SUPABASE_SERVICE_ROLE_KEY
)

app = FastAPI(title="EcoRota API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -------------------------------------------------------------
# SQLite Local Engine & Schema
# -------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000).hex()
    return hashed, salt

def init_db():
    conn = get_db()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        password_salt TEXT NOT NULL,
        display_name TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'resident',
        municipality TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS collection_points (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        materials TEXT NOT NULL,
        municipality TEXT NOT NULL,
        address TEXT,
        latitude REAL NOT NULL,
        longitude REAL NOT NULL,
        verified INTEGER NOT NULL DEFAULT 0,
        created_by TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS recycling_offers (
        id TEXT PRIMARY KEY,
        material TEXT NOT NULL,
        quantity TEXT NOT NULL,
        municipality TEXT NOT NULL,
        contact TEXT,
        notes TEXT,
        status TEXT NOT NULL DEFAULT 'pending',
        created_by TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS eco_actions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        action_type TEXT NOT NULL,
        description TEXT NOT NULL,
        points_awarded INTEGER NOT NULL DEFAULT 0,
        evidence_url TEXT,
        status TEXT NOT NULL DEFAULT 'approved',
        created_at TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS waste_analyses (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        category TEXT NOT NULL,
        confidence TEXT NOT NULL,
        visual_observation TEXT,
        guidance TEXT,
        care TEXT,
        mode TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """)

    # Seed or ensure ecoadmin user (Desenvolvedor Chefe)
    admin_user = c.execute("SELECT id FROM users WHERE email = ? OR email = ?", ("ecoadmin", "ecoadmin@ecorota.org")).fetchone()
    admin_hash, admin_salt = hash_password("ecomund2026")
    now_iso = datetime.now(timezone.utc).isoformat()
    if not admin_user:
        c.execute("""
            INSERT INTO users (id, email, password_hash, password_salt, display_name, role, municipality, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("ecoadmin_chief_001", "ecoadmin", admin_hash, admin_salt, "Desenvolvedor Chefe", "admin", "Central EcoRota", now_iso))
    else:
        c.execute("""
            UPDATE users SET password_hash = ?, password_salt = ?, role = 'admin', display_name = 'Desenvolvedor Chefe'
            WHERE id = ?
        """, (admin_hash, admin_salt, admin_user["id"]))

    # Seed initial collection points if empty
    res = c.execute("SELECT COUNT(*) as count FROM collection_points").fetchone()
    if res["count"] == 0:
        initial_points = [
            (
                secrets.token_hex(8),
                "Ecoponto Central Jijoca",
                "Plástico, Papel, Vidro, Metal, Eletrônicos",
                "Jijoca de Jericoacoara",
                "Av. Beira Mar, 100 - Centro",
                -2.8052,
                -40.5124,
                1,
                "system",
                datetime.now(timezone.utc).isoformat()
            ),
            (
                secrets.token_hex(8),
                "Ponto Verde Vila de Jericoacoara",
                "Vidro, Latas de Alumínio e Garrafas PET",
                "Jijoca de Jericoacoara",
                "Rua Principal, próx. à Praia",
                -2.7946,
                -40.5142,
                1,
                "system",
                datetime.now(timezone.utc).isoformat()
            ),
            (
                secrets.token_hex(8),
                "Associação de Recicladores Lagoa Azul",
                "Papelão, Plástico e Óleo de Cozinha Usado",
                "Cruz / Jijoca",
                "Estrada da Lagoa, km 4",
                -2.8210,
                -40.4980,
                1,
                "system",
                datetime.now(timezone.utc).isoformat()
            ),
            (
                secrets.token_hex(8),
                "Coleta Comunitária Mangue Seco",
                "Embalagens plásticas e sucata metálica",
                "Jijoca de Jericoacoara",
                "Comunidade Mangue Seco",
                -2.8120,
                -40.5350,
                1,
                "system",
                datetime.now(timezone.utc).isoformat()
            )
        ]
        c.executemany("""
            INSERT INTO collection_points (id, name, materials, municipality, address, latitude, longitude, verified, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, initial_points)

    conn.commit()
    conn.close()

init_db()

# -------------------------------------------------------------
# Supabase DB helper
# -------------------------------------------------------------
async def supabase_db(method: str, table: str, payload=None, query: str = ""):
    h = {
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, f"{SUPABASE_URL}/rest/v1/{table}{query}", headers=h, json=payload)
    if r.status_code >= 400:
        raise HTTPException(502, f"Falha no Supabase ({r.status_code}): {r.text}")
    return r.json() if r.content else []

# -------------------------------------------------------------
# Authentication & Session Handlers
# -------------------------------------------------------------
async def get_optional_user(authorization: Optional[str] = Header(default=None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    token = authorization.split(" ", 1)[1].strip()

    if USE_SUPABASE:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(
                    f"{SUPABASE_URL}/auth/v1/user",
                    headers={"apikey": SUPABASE_ANON_KEY, "Authorization": f"Bearer {token}"}
                )
            if r.status_code == 200:
                u = r.json()
                meta = u.get("user_metadata", {})
                return {
                    "id": u["id"],
                    "email": u.get("email"),
                    "display_name": meta.get("display_name", u.get("email", "Usuário")),
                    "role": meta.get("role", "resident"),
                    "municipality": meta.get("municipality", "")
                }
        except Exception:
            return None

    # SQLite session verification
    conn = get_db()
    c = conn.cursor()
    row = c.execute("""
        SELECT u.id, u.email, u.display_name, u.role, u.municipality
        FROM sessions s
        JOIN users u ON s.user_id = u.id
        WHERE s.token = ?
    """, (token,)).fetchone()
    conn.close()

    if row:
        return dict(row)
    return None

async def user_session(authorization: Optional[str] = Header(default=None)):
    user = await get_optional_user(authorization)
    if not user:
        raise HTTPException(401, "Faça login para continuar.")
    return user

async def admin_session(user: dict = Depends(user_session)):
    if user.get("role") != "admin":
        raise HTTPException(403, "Acesso restrito ao Desenvolvedor Chefe.")
    return user

# -------------------------------------------------------------
# Pydantic Request Models
# -------------------------------------------------------------
class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=6, max_length=100)
    display_name: str = Field(min_length=2, max_length=80)
    role: str = Field(default="resident")
    municipality: Optional[str] = "Jijoca de Jericoacoara"

class LoginRequest(BaseModel):
    email: str
    password: str

class Point(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    materials: str = Field(min_length=2, max_length=300)
    municipality: str = Field(min_length=2, max_length=100)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    address: Optional[str] = None
    verified: Optional[bool] = False

class Offer(BaseModel):
    material: str
    quantity: str
    municipality: str
    contact: Optional[str] = None
    notes: Optional[str] = None

class Action(BaseModel):
    action_type: str
    description: str
    points: Optional[int] = None
    evidence_url: Optional[str] = None

class RoleUpdateRequest(BaseModel):
    role: str

class StatusUpdateRequest(BaseModel):
    status: str

# -------------------------------------------------------------
# Web & Health Endpoints
# -------------------------------------------------------------
@app.get("/")
async def root():
    if INDEX_FILE.exists():
        return FileResponse(INDEX_FILE, media_type="text/html")
    return {"message": "EcoRota API 2.0 online"}

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "version": "2.0.0",
        "storage": "supabase" if USE_SUPABASE else "sqlite_local",
        "database_configured": True,
        "ai_configured": bool(GEMINI_API_KEY)
    }

# -------------------------------------------------------------
# Auth Endpoints
# -------------------------------------------------------------
@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    req.email = req.email.strip().lower()
    if USE_SUPABASE:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{SUPABASE_URL}/auth/v1/signup",
                headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
                json={
                    "email": req.email,
                    "password": req.password,
                    "data": {
                        "display_name": req.display_name,
                        "role": req.role,
                        "municipality": req.municipality
                    }
                }
            )
        if r.status_code >= 400:
            raise HTTPException(400, f"Erro no cadastro: {r.text}")
        data = r.json()
        token = data.get("access_token") or data.get("session", {}).get("access_token", "")
        return {
            "token": token,
            "user": {
                "id": data.get("user", {}).get("id", ""),
                "email": req.email,
                "display_name": req.display_name,
                "role": req.role,
                "municipality": req.municipality
            }
        }

    # SQLite
    conn = get_db()
    c = conn.cursor()
    existing = c.execute("SELECT id FROM users WHERE email = ?", (req.email,)).fetchone()
    if existing:
        conn.close()
        raise HTTPException(400, "Este e-mail já está cadastrado.")

    user_id = secrets.token_hex(16)
    pwd_hash, pwd_salt = hash_password(req.password)
    now = datetime.now(timezone.utc).isoformat()

    c.execute("""
        INSERT INTO users (id, email, password_hash, password_salt, display_name, role, municipality, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, req.email, pwd_hash, pwd_salt, req.display_name, req.role, req.municipality, now))

    token = secrets.token_urlsafe(32)
    c.execute("""
        INSERT INTO sessions (token, user_id, created_at)
        VALUES (?, ?, ?)
    """, (token, user_id, now))

    conn.commit()
    conn.close()

    return {
        "token": token,
        "user": {
            "id": user_id,
            "email": req.email,
            "display_name": req.display_name,
            "role": req.role,
            "municipality": req.municipality
        }
    }

@app.post("/api/auth/login")
async def login(req: LoginRequest):
    req.email = req.email.strip().lower()
    if USE_SUPABASE:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(
                f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
                headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
                json={"email": req.email, "password": req.password}
            )
        if r.status_code >= 400:
            raise HTTPException(401, "E-mail ou senha incorretos.")
        data = r.json()
        u = data.get("user", {})
        meta = u.get("user_metadata", {})
        return {
            "token": data["access_token"],
            "user": {
                "id": u["id"],
                "email": u.get("email"),
                "display_name": meta.get("display_name", u.get("email")),
                "role": meta.get("role", "resident"),
                "municipality": meta.get("municipality", "")
            }
        }

    # SQLite
    conn = get_db()
    c = conn.cursor()
    row = c.execute("""
        SELECT id, email, password_hash, password_salt, display_name, role, municipality
        FROM users WHERE email = ? OR (email = 'ecoadmin' AND ? = 'ecoadmin@ecorota.org')
    """, (req.email, req.email)).fetchone()

    if not row:
        conn.close()
        raise HTTPException(401, "E-mail ou senha incorretos.")

    test_hash, _ = hash_password(req.password, row["password_salt"])
    if test_hash != row["password_hash"]:
        conn.close()
        raise HTTPException(401, "E-mail ou senha incorretos.")

    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    c.execute("INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)", (token, row["id"], now))
    conn.commit()
    conn.close()

    return {
        "token": token,
        "user": {
            "id": row["id"],
            "email": row["email"],
            "display_name": row["display_name"],
            "role": row["role"],
            "municipality": row["municipality"]
        }
    }

@app.get("/api/me")
async def me(user: dict = Depends(user_session)):
    conn = get_db()
    c = conn.cursor()
    res = c.execute("SELECT COALESCE(SUM(points_awarded), 0) as total FROM eco_actions WHERE user_id = ?", (user["id"],)).fetchone()
    total_points = res["total"] if res else 0
    conn.close()

    user_info = dict(user)
    user_info["eco_points"] = total_points
    return user_info

# -------------------------------------------------------------
# EcoIA (Image Classification)
# -------------------------------------------------------------
@app.post("/api/classify")
async def classify(
    file: UploadFile = File(...),
    context: str = Form("residencial"),
    user: Optional[dict] = Depends(get_optional_user)
):
    image = await file.read()
    if len(image) > 8 * 1024 * 1024:
        raise HTTPException(413, "Imagem maior que 8 MB.")
    if file.content_type not in {"image/jpeg", "image/png", "image/webp", "image/jpg"}:
        raise HTTPException(415, "Formato não suportado. Use JPG, PNG ou WEBP.")

    result = None
    mode = "EcoIA (Gemini Visão)"

    if GEMINI_API_KEY:
        prompt = (
            f"Você é a EcoIA, sistema inteligente de triagem ambiental do projeto EcoRota. "
            f"Analise a imagem enviada no contexto '{context}'. "
            f"Identifique o resíduo/material apresentado. "
            f"Retorne OBRIGATORIAMENTE um objeto JSON puro, sem markdown e sem formatação adicional, contendo as seguintes chaves:\n"
            f"- 'category': Uma destas: 'Plástico', 'Papel/Papelão', 'Vidro', 'Metal', 'Orgânico', 'Eletrônico', 'Óleo de cozinha', 'Resíduo perigoso', 'Têxtil', 'Não identificado'.\n"
            f"- 'confidence': 'Alta', 'Média' ou 'Baixa'.\n"
            f"- 'visual_observation': Descrição objetiva de 1 frase do que foi visualizado na imagem.\n"
            f"- 'guidance': Orientação clara e prática sobre preparação e descarte adequado deste material.\n"
            f"- 'care': Cuidados de segurança e ambientais fundamentais (ex: higienização, perfurocortantes, separação).\n"
            f"Se houver dúvida ou o objeto não estiver visível, utilize 'Não identificado' com confiança 'Baixa'."
        )

        body = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": file.content_type, "data": base64.b64encode(image).decode()}}
                ]
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.1
            }
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

        try:
            async with httpx.AsyncClient(timeout=40) as client:
                r = await client.post(url, params={"key": GEMINI_API_KEY}, json=body)
            if r.status_code == 200:
                raw_text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                result = json.loads(raw_text)
                mode = f"EcoIA conectada ({GEMINI_MODEL})"
        except Exception:
            result = None

    if not result:
        name = (file.filename or "").lower()
        mode = "EcoIA Modo Local"
        if any(k in name for k in ["pet", "garrafa", "plast", "pote", "embalag", "sacola"]):
            cat = "Plástico"
            obs = "Embalagem polimérica identificada pelo perfil do material."
            guid = "Esvazie completamente, enxágue se houver resíduos orgânicos e compacte para reduzir volume."
            care = "Retire tampas e rótulos adesivos volumosos se possível para facilitar a reciclagem."
        elif any(k in name for k in ["papel", "papelao", "caixa", "folha", "jornal"]):
            cat = "Papel/Papelão"
            obs = "Material celulósico fibroso detectado."
            guid = "Mantenha seco e limpo. Dobre ou desmonte caixas para otimizar espaço de coleta."
            care = "Papéis engordurados ou com restos de comida devem ir para o lixo comum ou compostagem."
        elif any(k in name for k in ["vidro", "garrafa_v", "copo", "frasco"]):
            cat = "Vidro"
            obs = "Recipiente de vidro transparente ou colorido."
            guid = "Higienize com água e entregue em ecopontos com coleta seletiva de vidro."
            care = "Se estiver trincado ou quebrado, embale em caixa de papelão identificada para proteger coletores."
        elif any(k in name for k in ["lata", "metal", "alumin", "ferro", "cobre"]):
            cat = "Metal"
            obs = "Item metálico reciclável (alumínio/aço)."
            guid = "Esvazie o conteúdo e amasse latinhas de bebidas para transporte."
            care = "Atenção a rebarbas cortantes em latas de conserva abertas."
        elif any(k in name for k in ["resto", "fruta", "comida", "folhagem", "organ"]):
            cat = "Orgânico"
            obs = "Matéria orgânica biodegradável."
            guid = "Destine à composteira doméstica ou horta comunitária."
            care = "Evite misturar plásticos, vidros ou metais com matéria orgânica."
        elif any(k in name for k in ["oleo", "azeite", "fritura"]):
            cat = "Óleo de cozinha"
            obs = "Resíduo líquido gorduroso."
            guid = "Armazene frio em garrafa PET fechada e entregue em pontos de coleta especializada para sabão/biodiesel."
            care = "NUNCA descarte na pia ou solo, pois 1 litro contamina até 25.000 litros de água."
        else:
            cat = "Material sob análise"
            obs = "Material reciclável misto no contexto " + context + "."
            guid = "Verifique o símbolo de reciclagem no rótulo e consulte os ecopontos mais próximos no EcoMapa."
            care = "Separe secos de úmidos antes de enviar para a coleta seletiva."

        result = {
            "category": cat,
            "confidence": "Estimada",
            "visual_observation": obs,
            "guidance": guid,
            "care": care
        }

    # Save analysis in database
    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO waste_analyses (id, user_id, category, confidence, visual_observation, guidance, care, mode, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        secrets.token_hex(16),
        user["id"] if user else None,
        result.get("category", "Não identificado"),
        result.get("confidence", "Média"),
        result.get("visual_observation", ""),
        result.get("guidance", ""),
        result.get("care", ""),
        mode,
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    conn.close()

    result["mode"] = mode
    return result

# -------------------------------------------------------------
# EcoMapa (Collection Points)
# -------------------------------------------------------------
@app.get("/api/points")
async def get_points():
    if USE_SUPABASE:
        return await supabase_db("GET", "collection_points", query="?select=id,name,materials,municipality,address,latitude,longitude,verified,created_at&order=created_at.desc")

    conn = get_db()
    c = conn.cursor()
    rows = c.execute("""
        SELECT id, name, materials, municipality, address, latitude, longitude, verified, created_at
        FROM collection_points
        ORDER BY created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/points")
async def add_point(item: Point, user: Optional[dict] = Depends(get_optional_user)):
    user_id = user["id"] if user else None
    is_verified = 1 if (user and user.get("role") == "admin") or item.verified else 0

    if USE_SUPABASE:
        p = item.model_dump()
        p.update(created_by=user_id, verified=bool(is_verified))
        return await supabase_db("POST", "collection_points", p)

    point_id = secrets.token_hex(16)
    now = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO collection_points (id, name, materials, municipality, address, latitude, longitude, verified, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        point_id, item.name, item.materials, item.municipality, item.address or "",
        item.latitude, item.longitude, is_verified, user_id, now
    ))
    conn.commit()
    conn.close()

    return {
        "id": point_id,
        "name": item.name,
        "materials": item.materials,
        "municipality": item.municipality,
        "latitude": item.latitude,
        "longitude": item.longitude,
        "verified": bool(is_verified),
        "created_at": now
    }

# -------------------------------------------------------------
# Conexão Verde (Recycling Offers)
# -------------------------------------------------------------
@app.get("/api/offers")
async def get_offers():
    if USE_SUPABASE:
        return await supabase_db("GET", "recycling_offers", query="?select=id,material,quantity,municipality,contact,notes,status,created_at&order=created_at.desc")

    conn = get_db()
    c = conn.cursor()
    rows = c.execute("""
        SELECT id, material, quantity, municipality, contact, notes, status, created_at
        FROM recycling_offers
        ORDER BY created_at DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/offers")
async def add_offer(item: Offer, user: Optional[dict] = Depends(get_optional_user)):
    user_id = user["id"] if user else None

    if USE_SUPABASE:
        p = item.model_dump()
        p.update(created_by=user_id, status="pending")
        return await supabase_db("POST", "recycling_offers", p)

    offer_id = secrets.token_hex(16)
    now = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO recycling_offers (id, material, quantity, municipality, contact, notes, status, created_by, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        offer_id, item.material, item.quantity, item.municipality, item.contact or "",
        item.notes or "", "pending", user_id, now
    ))
    conn.commit()
    conn.close()

    return {
        "id": offer_id,
        "material": item.material,
        "quantity": item.quantity,
        "municipality": item.municipality,
        "contact": item.contact,
        "status": "pending",
        "created_at": now
    }

# -------------------------------------------------------------
# EcoPontos & Ações
# -------------------------------------------------------------
@app.get("/api/actions")
async def get_actions(user: dict = Depends(user_session)):
    if USE_SUPABASE:
        return await supabase_db("GET", "eco_actions", query=f"?select=id,action_type,description,points_awarded,status,created_at&user_id=eq.{user['id']}&order=created_at.desc")

    conn = get_db()
    c = conn.cursor()
    rows = c.execute("""
        SELECT id, action_type, description, points_awarded, status, created_at
        FROM eco_actions
        WHERE user_id = ?
        ORDER BY created_at DESC
    """, (user["id"],)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/actions")
async def add_action(item: Action, user: dict = Depends(user_session)):
    points = item.points if item.points is not None else 20
    if item.action_type == "mutirao":
        points = 30
    elif item.action_type == "ponto":
        points = 15

    if USE_SUPABASE:
        p = {
            "action_type": item.action_type,
            "description": item.description,
            "evidence_url": item.evidence_url,
            "user_id": user["id"],
            "status": "approved",
            "points_awarded": points
        }
        return await supabase_db("POST", "eco_actions", p)

    action_id = secrets.token_hex(16)
    now = datetime.now(timezone.utc).isoformat()

    conn = get_db()
    c = conn.cursor()
    c.execute("""
        INSERT INTO eco_actions (id, user_id, action_type, description, points_awarded, evidence_url, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        action_id, user["id"], item.action_type, item.description, points,
        item.evidence_url or "", "approved", now
    ))
    conn.commit()
    conn.close()

    return {
        "id": action_id,
        "action_type": item.action_type,
        "description": item.description,
        "points_awarded": points,
        "status": "approved",
        "created_at": now
    }

# -------------------------------------------------------------
# Painel Ambiental / Estatísticas
# -------------------------------------------------------------
@app.get("/api/stats")
async def get_stats():
    conn = get_db()
    c = conn.cursor()

    pt_count = c.execute("SELECT COUNT(*) as c FROM collection_points").fetchone()["c"]
    off_count = c.execute("SELECT COUNT(*) as c FROM recycling_offers").fetchone()["c"]
    act_count = c.execute("SELECT COUNT(*) as c FROM eco_actions").fetchone()["c"]
    an_count = c.execute("SELECT COUNT(*) as c FROM waste_analyses").fetchone()["c"]
    total_pts = c.execute("SELECT COALESCE(SUM(points_awarded), 0) as s FROM eco_actions").fetchone()["s"]

    # Categories breakdown
    categories = ["Plástico", "Papel", "Vidro", "Metal", "Orgânico", "Outros"]
    breakdown = {}
    for cat in categories:
        if cat == "Outros":
            cnt = c.execute("""
                SELECT COUNT(*) as c FROM waste_analyses
                WHERE category NOT LIKE '%Plástico%'
                  AND category NOT LIKE '%Papel%'
                  AND category NOT LIKE '%Vidro%'
                  AND category NOT LIKE '%Metal%'
                  AND category NOT LIKE '%Orgânico%'
            """).fetchone()["c"]
        else:
            cnt = c.execute("""
                SELECT COUNT(*) as c FROM waste_analyses
                WHERE category LIKE ?
            """, (f"%{cat}%",)).fetchone()["c"]
        breakdown[cat] = cnt

    conn.close()

    return {
        "points_count": pt_count,
        "offers_count": off_count,
        "actions_count": act_count,
        "analyses_count": an_count,
        "total_registrations": act_count + an_count,
        "total_eco_points": total_pts,
        "categories": breakdown
    }

# -------------------------------------------------------------
# Export Data (CSV)
# -------------------------------------------------------------
@app.get("/api/export")
async def export_csv():
    conn = get_db()
    c = conn.cursor()

    rows = [["tipo", "identificador_ou_nome", "detalhes", "data_registro"]]

    # Points
    for p in c.execute("SELECT name, materials, municipality, created_at FROM collection_points").fetchall():
        rows.append(["ponto_coleta", p["name"], f"{p['materials']} ({p['municipality']})", p["created_at"]])

    # Offers
    for o in c.execute("SELECT material, quantity, municipality, created_at FROM recycling_offers").fetchall():
        rows.append(["oferta_reciclagem", o["material"], f"{o['quantity']} - {o['municipality']}", o["created_at"]])

    # Actions
    for a in c.execute("SELECT action_type, description, points_awarded, created_at FROM eco_actions").fetchall():
        rows.append(["acao_sustentavel", a["action_type"], f"{a['description']} (+{a['points_awarded']} EP)", a["created_at"]])

    # Analyses
    for w in c.execute("SELECT category, guidance, created_at FROM waste_analyses").fetchall():
        rows.append(["analise_ecoia", w["category"], w["guidance"][:60], w["created_at"]])

    conn.close()

    csv_lines = [";".join(f'"{str(col).replace(chr(34), chr(34)+chr(34))}"' for col in r) for r in rows]
    csv_content = "\ufeff" + "\n".join(csv_lines)

    return Response(
        content=csv_content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=ecorota-dados-completos.csv"}
    )

# -------------------------------------------------------------
# ABA DO DESENVOLVEDOR CHEFE (Painel Administrativo & Banco)
# -------------------------------------------------------------
@app.get("/api/admin/data")
async def admin_get_data(admin: dict = Depends(admin_session)):
    conn = get_db()
    c = conn.cursor()

    users = [dict(r) for r in c.execute("SELECT id, email, display_name, role, municipality, created_at FROM users ORDER BY created_at DESC").fetchall()]
    points = [dict(r) for r in c.execute("SELECT * FROM collection_points ORDER BY created_at DESC").fetchall()]
    offers = [dict(r) for r in c.execute("SELECT * FROM recycling_offers ORDER BY created_at DESC").fetchall()]
    actions = [dict(r) for r in c.execute("""
        SELECT a.*, u.display_name as user_name, u.email as user_email
        FROM eco_actions a
        LEFT JOIN users u ON a.user_id = u.id
        ORDER BY a.created_at DESC
    """).fetchall()]
    analyses = [dict(r) for r in c.execute("SELECT * FROM waste_analyses ORDER BY created_at DESC").fetchall()]

    conn.close()

    return {
        "admin": admin["display_name"],
        "tables": {
            "users": users,
            "collection_points": points,
            "recycling_offers": offers,
            "eco_actions": actions,
            "waste_analyses": analyses
        }
    }

@app.post("/api/admin/points/{point_id}/toggle-verify")
async def admin_toggle_verify_point(point_id: str, admin: dict = Depends(admin_session)):
    conn = get_db()
    c = conn.cursor()
    row = c.execute("SELECT verified FROM collection_points WHERE id = ?", (point_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "Ponto não encontrado.")
    new_status = 0 if row["verified"] else 1
    c.execute("UPDATE collection_points SET verified = ? WHERE id = ?", (new_status, point_id))
    conn.commit()
    conn.close()
    return {"id": point_id, "verified": bool(new_status)}

@app.delete("/api/admin/points/{point_id}")
async def admin_delete_point(point_id: str, admin: dict = Depends(admin_session)):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM collection_points WHERE id = ?", (point_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": point_id}

@app.put("/api/admin/offers/{offer_id}/status")
async def admin_update_offer_status(offer_id: str, req: StatusUpdateRequest, admin: dict = Depends(admin_session)):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE recycling_offers SET status = ? WHERE id = ?", (req.status, offer_id))
    conn.commit()
    conn.close()
    return {"id": offer_id, "status": req.status}

@app.delete("/api/admin/offers/{offer_id}")
async def admin_delete_offer(offer_id: str, admin: dict = Depends(admin_session)):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM recycling_offers WHERE id = ?", (offer_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": offer_id}

@app.put("/api/admin/actions/{action_id}/status")
async def admin_update_action_status(action_id: str, req: StatusUpdateRequest, admin: dict = Depends(admin_session)):
    conn = get_db()
    c = conn.cursor()
    points = 20 if req.status == "approved" else 0
    c.execute("UPDATE eco_actions SET status = ?, points_awarded = ? WHERE id = ?", (req.status, points, action_id))
    conn.commit()
    conn.close()
    return {"id": action_id, "status": req.status, "points_awarded": points}

@app.delete("/api/admin/actions/{action_id}")
async def admin_delete_action(action_id: str, admin: dict = Depends(admin_session)):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM eco_actions WHERE id = ?", (action_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": action_id}

@app.put("/api/admin/users/{user_id}/role")
async def admin_update_user_role(user_id: str, req: RoleUpdateRequest, admin: dict = Depends(admin_session)):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE users SET role = ? WHERE id = ?", (req.role, user_id))
    conn.commit()
    conn.close()
    return {"id": user_id, "role": req.role}

@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: str, admin: dict = Depends(admin_session)):
    if user_id == admin["id"] or user_id == "ecoadmin_chief_001":
        raise HTTPException(400, "Você não pode excluir o Desenvolvedor Chefe principal.")
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": user_id}

@app.delete("/api/admin/analyses/{analysis_id}")
async def admin_delete_analysis(analysis_id: str, admin: dict = Depends(admin_session)):
    conn = get_db()
    c = conn.cursor()
    c.execute("DELETE FROM waste_analyses WHERE id = ?", (analysis_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted", "id": analysis_id}

@app.get("/api/admin/backup-json")
async def admin_backup_json(admin: dict = Depends(admin_session)):
    conn = get_db()
    c = conn.cursor()
    data = {
        "backup_date": datetime.now(timezone.utc).isoformat(),
        "users": [dict(r) for r in c.execute("SELECT id, email, display_name, role, municipality, created_at FROM users").fetchall()],
        "collection_points": [dict(r) for r in c.execute("SELECT * FROM collection_points").fetchall()],
        "recycling_offers": [dict(r) for r in c.execute("SELECT * FROM recycling_offers").fetchall()],
        "eco_actions": [dict(r) for r in c.execute("SELECT * FROM eco_actions").fetchall()],
        "waste_analyses": [dict(r) for r in c.execute("SELECT * FROM waste_analyses").fetchall()]
    }
    conn.close()
    return Response(
        content=json.dumps(data, indent=2, ensure_ascii=False),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=ecorota-backup-completo.json"}
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
