from collections import deque
import json
import logging
import hashlib
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from threading import Lock
from uuid import uuid4
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError, VerificationError
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy import func, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from shared.auth_tokens import authenticated_claims, issue_access_token, read_private_key
from database import Base, SessionLocal, engine, get_db
from models import RefreshSession, Usuario

PASSWORDS = PasswordHasher()
DUMMY_PASSWORD_HASH = PASSWORDS.hash("not-a-real-account-password")
REFRESH_COOKIE = "nexustix_refresh"
REFRESH_DAYS = int(os.getenv("REFRESH_TOKEN_TTL_DAYS", "30"))
AUTH_MIGRATION_REVISION = "20261102_02"


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nombre: str = Field(min_length=1, max_length=160)
    email: EmailStr
    password: str = Field(min_length=12, max_length=256)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)


def esperar_bd():
    """Check connection and refuse to start unless the explicit migration is applied."""
    intentos = 10
    while intentos:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none()
            if revision != AUTH_MIGRATION_REVISION:
                raise RuntimeError("La base de Usuarios requiere la migración de autenticación aprobada")
            return
        except ProgrammingError as exc:
            raise RuntimeError(
                "La base de Usuarios no tiene esquema migrado; ejecute 'python -m shared.run_migrations'"
            ) from exc
        except OperationalError:
            intentos -= 1
            time.sleep(3)
    raise RuntimeError("No se pudo conectar a la base de datos de usuarios")


@asynccontextmanager
async def _lifespan(application: FastAPI):
    # No se crean tablas ni se siembran cuentas automáticamente. El esquema
    # debe haber sido migrado explícitamente antes de desplegar esta versión.
    esperar_bd()
    yield


app = FastAPI(title="Servicio de Usuarios", lifespan=_lifespan)
FRONTEND_ORIGINS = [origin.strip() for origin in os.getenv("FRONTEND_ORIGINS", "http://localhost:8080,http://127.0.0.1:8080").split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Protection"],
)

RELOJ_LAMPORT = 0
_RELOJ_LOCK = Lock()


def tic_lamport(t_recibido: int = 0) -> int:
    global RELOJ_LAMPORT
    with _RELOJ_LOCK:
        RELOJ_LAMPORT = max(RELOJ_LAMPORT, t_recibido) + 1
        return RELOJ_LAMPORT


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cookie_secure() -> bool:
    return os.getenv("AUTH_COOKIE_SECURE", "true").lower() in {"1", "true", "yes"}


def _set_refresh_cookie(response: Response, token: str):
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=REFRESH_DAYS * 24 * 60 * 60,
        httponly=True,
        secure=_cookie_secure(),
        samesite=os.getenv("AUTH_COOKIE_SAMESITE", "strict").lower(),
        path="/auth",
    )


def _clear_refresh_cookie(response: Response):
    response.delete_cookie(REFRESH_COOKIE, path="/auth", httponly=True, secure=_cookie_secure(), samesite="strict")


def _csrf_check(request: Request, x_csrf_protection: str | None):
    origin = request.headers.get("origin")
    if origin not in FRONTEND_ORIGINS or x_csrf_protection != "1":
        raise HTTPException(status_code=403, detail="Verificación CSRF fallida")


def _new_session(db: Session, user: Usuario, family_id: str | None = None):
    token = secrets.token_urlsafe(48)
    token_hash = _token_hash(token)
    row = RefreshSession(
        id=uuid4().hex,
        family_id=family_id or uuid4().hex,
        user_id=user.id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(days=REFRESH_DAYS),
    )
    db.add(row)
    return token, row


def _access_token(user: Usuario) -> str:
    try:
        return issue_access_token(user.id, user.role, read_private_key())
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="Firma de tokens no configurada correctamente") from exc


def _auth_response(user: Usuario, refresh_token: str, access_token: str):
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", "600")),
        "user": {"id": user.id, "nombre": user.nombre, "email": user.email, "role": user.role},
    }


def _finish_auth_response(user: Usuario, refresh_token: str, access_token: str, response: Response):
    result = _auth_response(user, refresh_token, access_token)
    _set_refresh_cookie(response, refresh_token)
    return result


def get_authenticated_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    claims = authenticated_claims(authorization)
    try:
        user_id = int(claims["sub"])
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=401, detail="Token inválido") from exc
    user = db.query(Usuario).filter(Usuario.id == user_id).first()
    if not user or user.auth_status != "active" or user.estado != "activo":
        raise HTTPException(status_code=401, detail="Cuenta inactiva o sin acceso autenticado")
    return user


# La tabla de Usuarios exige una revisión exacta: este registro es por instancia y no persiste.
ROLE_AUDIT = deque(maxlen=100)


def security_event(action, resource, result, user):
    entry = {"accion": action, "recurso": resource, "resultado": result, "actor": str(user.id), "rol": user.role,
             "momento": datetime.now(timezone.utc).isoformat(), "instancia": os.getenv("INSTANCE_ID", "usuarios")}
    ROLE_AUDIT.append(entry)
    logging.getLogger("nexustix.audit").warning(json.dumps(entry))


def require_admin(user: Usuario = Depends(get_authenticated_user)):
    if user.role != "administrador":
        security_event("acceso_administrativo", "usuarios", "denegado", user)
        raise HTTPException(status_code=403, detail="Se requiere rol administrador")
    return user


@app.post("/auth/register", status_code=201)
def register(datos: RegisterRequest, response: Response, db: Session = Depends(get_db)):
    email = str(datos.email).strip().lower()
    nombre = datos.nombre.strip()
    if not nombre:
        raise HTTPException(status_code=422, detail="El nombre no puede estar vacío")
    if db.query(Usuario.id).filter(func.lower(func.trim(Usuario.email)) == email).first():
        raise HTTPException(status_code=409, detail="El correo ya está registrado")
    user = Usuario(
        nombre=nombre,
        email=email,
        estado="activo",
        password_hash=PASSWORDS.hash(datos.password),
        role="comprador",
        auth_status="active",
    )
    try:
        db.add(user)
        db.flush()
        access_token = _access_token(user)
        refresh_token, _ = _new_session(db, user)
        db.commit()
        db.refresh(user)
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="El correo ya está registrado") from exc
    return _finish_auth_response(user, refresh_token, access_token, response)


@app.post("/auth/login")
def login(datos: LoginRequest, response: Response, db: Session = Depends(get_db)):
    email = str(datos.email).strip().lower()
    user = db.query(Usuario).filter(func.lower(func.trim(Usuario.email)) == email).first()
    password_hash = user.password_hash if user and user.password_hash else DUMMY_PASSWORD_HASH
    try:
        valid_password = PASSWORDS.verify(password_hash, datos.password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        valid_password = False
    if not valid_password or not user or user.auth_status != "active" or user.estado != "activo":
        raise HTTPException(status_code=401, detail="Correo o contraseña incorrectos")
    access_token = _access_token(user)
    refresh_token, _ = _new_session(db, user)
    db.commit()
    return _finish_auth_response(user, refresh_token, access_token, response)


@app.post("/auth/refresh")
def refresh_session(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    x_csrf_protection: str | None = Header(default=None, alias="X-CSRF-Protection"),
):
    _csrf_check(request, x_csrf_protection)
    raw_token = request.cookies.get(REFRESH_COOKIE)
    if not raw_token:
        raise HTTPException(status_code=401, detail="Sesión de actualización no disponible")
    token_hash = _token_hash(raw_token)
    session = db.query(RefreshSession).filter(RefreshSession.token_hash == token_hash).with_for_update().first()
    now = datetime.now(timezone.utc)
    if not session:
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada")
    if session.revoked_at is not None:
        db.query(RefreshSession).filter(
            RefreshSession.family_id == session.family_id,
            RefreshSession.revoked_at.is_(None),
        ).update({RefreshSession.revoked_at: now}, synchronize_session=False)
        db.commit()
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Se detectó reutilización de una sesión revocada")
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= now:
        session.revoked_at = now
        db.commit()
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Sesión inválida o expirada")
    user = db.query(Usuario).filter(Usuario.id == session.user_id).first()
    if not user or user.auth_status != "active" or user.estado != "activo":
        session.revoked_at = now
        db.commit()
        _clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail="Cuenta inactiva")
    access_token = _access_token(user)
    new_token, new_session = _new_session(db, user, session.family_id)
    session.revoked_at = now
    session.replaced_by_hash = _token_hash(new_token)
    db.add(new_session)
    db.commit()
    return _finish_auth_response(user, new_token, access_token, response)


@app.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    x_csrf_protection: str | None = Header(default=None, alias="X-CSRF-Protection"),
):
    _csrf_check(request, x_csrf_protection)
    raw_token = request.cookies.get(REFRESH_COOKIE)
    if raw_token:
        row = db.query(RefreshSession).filter(RefreshSession.token_hash == _token_hash(raw_token)).first()
        if row and row.revoked_at is None:
            row.revoked_at = datetime.now(timezone.utc)
            db.commit()
    _clear_refresh_cookie(response)
    return {"status": "sesion_cerrada"}


@app.get("/auth/me")
def me(user: Usuario = Depends(get_authenticated_user)):
    return {"id": user.id, "nombre": user.nombre, "email": user.email, "role": user.role}


@app.get("/health")
def health(response: Response, db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        db.rollback()
        response.status_code = 503
        return {"status": "unavailable", "servicio": "usuarios", "db": "unavailable"}
    return {"status": "ok", "servicio": "usuarios", "db": "connected", "reloj_logico_T": tic_lamport(), "timestamp_fisico": time.time()}


@app.get("/usuarios")
def listar_usuarios(db: Session = Depends(get_db), _admin: Usuario = Depends(require_admin)):
    return [{"id": user.id, "nombre": user.nombre, "email": user.email, "estado": user.estado, "role": user.role} for user in db.query(Usuario).all()]


@app.get("/usuarios/{user_id}")
def obtener_usuario(user_id: int, db: Session = Depends(get_db), actor: Usuario = Depends(get_authenticated_user)):
    if actor.id != user_id and actor.role != "administrador":
        raise HTTPException(status_code=403, detail="No puedes consultar otro perfil")
    user = db.query(Usuario).filter(Usuario.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return {"id": user.id, "nombre": user.nombre, "email": user.email, "estado": user.estado, "role": user.role}


class RoleRequest(BaseModel):
    model_config = {"extra": "forbid"}
    role: Literal["comprador", "organizador"]


@app.post("/usuarios/{user_id}/rol")
def assign_role(user_id: int, data: RoleRequest, db: Session = Depends(get_db), admin: Usuario = Depends(require_admin)):
    user = db.query(Usuario).filter_by(id=user_id).with_for_update().first()
    if not user:
        raise HTTPException(404, "Usuario no encontrado")
    if user.role == "administrador":
        raise HTTPException(409, "No se degradan administradores desde esta API")
    user.role = data.role
    # Revoca sesiones de refresco anteriores; tokens de acceso duran hasta su exp.
    db.query(RefreshSession).filter_by(user_id=user_id, revoked_at=None).update({"revoked_at": datetime.now(timezone.utc)})
    db.commit()
    security_event("cambiar_rol", f"usuario:{user.id}:{user.role}", "ejecutado", admin)
    return {"id": user.id, "role": user.role, "requiere_nuevo_login": True}


@app.get("/auditoria/roles")
def role_audit(_admin: Usuario = Depends(require_admin)):
    return {"registros": list(ROLE_AUDIT), "alcance": "Memoria de esta instancia; se pierde al reiniciarla. El registro duradero son sus logs."}



from shared import telemetry
telemetry.install(app, "usuarios")

from shared import diagnostics
diagnostics.install(app, get_db, require_admin)
