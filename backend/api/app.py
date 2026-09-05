from datetime import UTC, datetime
from typing import Annotated
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from backend.auth import current_actor, hash_password, make_token, require, verify_password
from backend.db import SessionFactory
from backend.models import Admin, User
from backend.services.orders import Actor, CreateOrderCommand, IdempotencyConflict, OrderService

app = FastAPI(title="OnlineShop API", version="1.0.0")
ROUTE_PERMISSIONS = {"POST /api/v1/auth/token": "public", "POST /api/v1/orders": "owner|manager|seller", "GET /api/v1/orders/{public_id}": "owner|manager|seller|warehouse", "GET /api/v1/orders": "owner|manager|seller|warehouse", "POST /api/v1/admins": "owner", "PATCH /api/v1/admins/{telegram_id}": "owner", "GET /health/live": "public", "GET /health/ready": "public"}

class Strict(BaseModel): model_config = ConfigDict(extra="forbid")
class OrderIn(Strict):
    customer_name: str; phone_raw: str; province: str; city: str; address: str; postal_code: str | None = None
    product_raw: str; quantity: int = Field(gt=0, le=100000); amount: int | None = Field(default=None, ge=0); notes: str | None = None; photo_file_id: str = ""; idempotency_key: str = Field(min_length=1, max_length=200); allow_duplicate: bool = False
class AdminIn(Strict): telegram_id: int = Field(gt=0); name: str; admin_code: str
class ActiveIn(Strict): active: bool
class UserIn(Strict): username: str = Field(min_length=1, max_length=120); password: str = Field(min_length=12); role: str; telegram_id: int | None = None
class OrderOut(Strict): public_id: str; created_at: datetime; delivery_status: str; delivery_attempts: int; delivery_error: str | None = None; customer_name: str | None = None; phone_raw: str | None = None; address: str | None = None; product_raw: str | None = None; quantity: int | None = None; amount: int | None = None
class Token(Strict): access_token: str; token_type: str

def output(order, actor):
    seller = actor.role == "seller"
    warehouse = actor.role == "warehouse"
    fields = {} if seller else {"customer_name": order.customer_name, "phone_raw": order.phone_raw, "address": order.address, "product_raw": order.product_raw, "quantity": order.quantity, "amount": order.amount}
    if warehouse: fields = {"address": order.address, "product_raw": order.product_raw, "quantity": order.quantity}
    return OrderOut(public_id=order.public_id, created_at=order.created_at, delivery_status=order.delivery_status, delivery_attempts=order.delivery_attempts, delivery_error=None if warehouse else order.delivery_error, **fields)

@app.exception_handler(ValueError)
async def value_error(_, exc): return JSONResponse(status_code=422, content={"detail": str(exc)})
@app.exception_handler(IdempotencyConflict)
async def idempotency_conflict(_, exc): return JSONResponse(status_code=409, content={"detail": str(exc)})
@app.exception_handler(PermissionError)
async def permission_error(_, exc): return JSONResponse(status_code=403, content={"detail": str(exc)})

@app.post("/api/v1/auth/token", response_model=Token)
async def token(form: Annotated[OAuth2PasswordRequestForm, Depends()]):
    async with SessionFactory() as s:
        user = await s.scalar(select(User).where(User.username == form.username))
        try: valid = user is not None and verify_password(form.password, user.password_hash)
        except Exception: valid = False
        if not valid or not user.is_active: raise HTTPException(status_code=401, detail="incorrect credentials")
        return Token(access_token=make_token(user), token_type="bearer")

@app.post("/api/v1/orders", response_model=OrderOut, response_model_exclude_none=True)
async def create_order(payload: OrderIn, actor: Actor = Depends(require("owner", "manager", "seller"))):
    from order_bot.validation import normalize_phone, normalize_product
    async with SessionFactory() as s:
        result = await OrderService(s).create_order(CreateOrderCommand(payload.customer_name, payload.phone_raw, normalize_phone(payload.phone_raw), payload.province, payload.city, payload.address, payload.postal_code, payload.product_raw, normalize_product(payload.product_raw), payload.quantity, payload.amount, payload.notes, payload.photo_file_id, payload.idempotency_key, payload.allow_duplicate), actor)
        if result.duplicate_confirmation_required: raise HTTPException(409, "duplicate confirmation required")
        return output(result.order, actor)

@app.get("/api/v1/orders/{public_id}", response_model=OrderOut, response_model_exclude_none=True)
async def get_order(public_id: str, actor: Actor = Depends(require("owner", "manager", "seller", "warehouse"))):
    async with SessionFactory() as s:
        order = await OrderService(s).get_order(public_id, actor)
        if order is None: raise HTTPException(404, "order not found")
        return output(order, actor)

@app.get("/api/v1/orders", response_model=list[OrderOut], response_model_exclude_none=True)
async def list_orders(limit: int = Query(50, ge=1, le=100), cursor: int | None = Query(None, ge=1), actor: Actor = Depends(require("owner", "manager", "seller", "warehouse"))):
    async with SessionFactory() as s: return [output(o, actor) for o in await OrderService(s).list_orders(actor, limit=limit, cursor=cursor)]

@app.post("/api/v1/admins", status_code=201)
async def add_admin(payload: AdminIn, actor: Actor = Depends(require("owner"))):
    async with SessionFactory() as s:
        admin = Admin(telegram_id=payload.telegram_id, name=payload.name, admin_code=payload.admin_code.upper(), is_active=True, created_at=datetime.now(UTC)); s.add(admin)
        try: await s.commit()
        except Exception: await s.rollback(); raise HTTPException(409, "admin already exists")
    return {"telegram_id": admin.telegram_id, "name": admin.name, "admin_code": admin.admin_code, "is_active": True}

@app.post("/api/v1/users", status_code=201)
async def add_user(payload: UserIn, actor: Actor = Depends(require("owner"))):
    if payload.role not in {"owner", "manager", "seller", "warehouse"}: raise HTTPException(422, "invalid role")
    async with SessionFactory() as s:
        user = User(username=payload.username, password_hash=hash_password(payload.password), role=payload.role, telegram_id=payload.telegram_id, is_active=True, token_version=0); s.add(user)
        try: await s.commit()
        except Exception: await s.rollback(); raise HTTPException(409, "user already exists")
    return {"id": user.id, "username": user.username, "role": user.role, "is_active": True}

@app.patch("/api/v1/users/{user_id}/revoke")
async def revoke_user(user_id: int, actor: Actor = Depends(require("owner"))):
    async with SessionFactory() as s:
        user = await s.get(User, user_id)
        if user is None: raise HTTPException(404, "user not found")
        user.is_active = False; user.token_version += 1; await s.commit()
    return {"id": user_id, "is_active": False}

@app.patch("/api/v1/admins/{telegram_id}")
async def set_admin(telegram_id: int, payload: ActiveIn, actor: Actor = Depends(require("owner"))):
    async with SessionFactory() as s:
        admin = await s.get(Admin, telegram_id)
        if admin is None: raise HTTPException(404, "admin not found")
        admin.is_active = payload.active; await s.commit()
        user = await s.scalar(select(User).where(User.telegram_id == telegram_id))
        if user and not payload.active:
            user.is_active = False; user.token_version += 1; await s.commit()
    return {"telegram_id": telegram_id, "is_active": payload.active}

@app.get("/health/live")
async def live(): return {"status": "ok"}
@app.get("/health/ready")
async def ready():
    try:
        async with SessionFactory() as s: await s.execute(select(1))
    except Exception: raise HTTPException(503, "database unavailable")
    return {"status": "ok"}
