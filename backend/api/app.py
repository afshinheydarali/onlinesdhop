from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.security import OAuth2PasswordRequestForm
from pwdlib.exceptions import UnknownHashError
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.auth import (
    hash_password_async,
    make_token,
    require,
    verify_password_async,
)
from backend.db import SessionFactory
from backend.models import Admin, InventoryBalance, Order, Product, User
from backend.services.orders import (
    Actor,
    CartLine,
    CreateCartOrderCommand,
    CreateOrderCommand,
    IdempotencyConflict,
    OrderService,
)

app = FastAPI(title="OnlineShop API", version="1.0.0")
ROUTE_PERMISSIONS = {
    "POST /api/v1/auth/token": "public",
    "POST /api/v1/orders": "owner|manager|seller",
    "POST /api/v1/commerce/orders": "owner|manager|seller",
    "POST /api/v1/products": "owner|manager",
    "GET /api/v1/products": "owner|manager|seller|warehouse",
    "GET /api/v1/orders/{public_id}": "owner|manager|seller|warehouse",
    "GET /api/v1/orders": "owner|manager|seller|warehouse",
    "POST /api/v1/admins": "owner",
    "PATCH /api/v1/admins/{telegram_id}": "owner",
    "POST /api/v1/users": "owner",
    "GET /api/v1/users": "owner",
    "PATCH /api/v1/users/{user_id}/revoke": "owner",
    "GET /health/live": "public",
    "GET /health/ready": "public",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderIn(Strict):
    customer_name: str = Field(min_length=1, max_length=120)
    phone_raw: str = Field(min_length=1, max_length=30)
    province: str = Field(min_length=1, max_length=80)
    city: str = Field(min_length=1, max_length=80)
    address: str = Field(min_length=1, max_length=600)
    postal_code: str | None = Field(default=None, max_length=30)
    product_raw: str = Field(min_length=1, max_length=200)
    quantity: StrictInt = Field(gt=0, le=100000)
    amount: StrictInt | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, max_length=1000)
    photo_file_id: str = Field(default="", max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=200)
    allow_duplicate: bool = False


class CartItemIn(Strict):
    sku: str = Field(min_length=1, max_length=80)
    quantity: StrictInt = Field(gt=0, le=100000)


class CartOrderIn(Strict):
    customer_name: str = Field(min_length=1, max_length=120)
    phone_raw: str = Field(min_length=1, max_length=30)
    province: str = Field(min_length=1, max_length=80)
    city: str = Field(min_length=1, max_length=80)
    address: str = Field(min_length=1, max_length=600)
    postal_code: str | None = Field(default=None, max_length=30)
    items: list[CartItemIn] = Field(min_length=1, max_length=100)
    notes: str | None = Field(default=None, max_length=1000)
    idempotency_key: str = Field(min_length=1, max_length=200)


class ProductIn(Strict):
    sku: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    unit_price: StrictInt = Field(ge=0, le=10**15)
    currency: str = Field(default="IRR", min_length=3, max_length=3)
    on_hand: StrictInt = Field(default=0, ge=0, le=2**31 - 1)


class AdminIn(Strict):
    telegram_id: int = Field(gt=0, le=2**63 - 1)
    name: str = Field(min_length=1, max_length=120)
    admin_code: str = Field(min_length=1, max_length=32)


class ActiveIn(Strict):
    active: bool


class UserIn(Strict):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=12)
    role: str
    telegram_id: int | None = None


class OrderOut(Strict):
    public_id: str
    created_at: datetime
    delivery_status: str
    delivery_attempts: int
    delivery_error: str | None = None
    customer_name: str | None = None
    phone_raw: str | None = None
    address: str | None = None
    product_raw: str | None = None
    quantity: int | None = None
    amount: int | None = None
    currency: str | None = None


class Token(Strict):
    access_token: str
    token_type: str


class OrderPage(Strict):
    items: list[OrderOut]
    next_cursor: int | None = None


def output(order: Order, actor: Actor) -> OrderOut:
    seller = actor.role == "seller"
    warehouse = actor.role == "warehouse"
    fields = (
        {}
        if seller
        else {
            "customer_name": order.customer_name,
            "phone_raw": order.phone_raw,
            "address": order.address,
            "product_raw": order.product_raw,
            "quantity": order.quantity,
            "amount": order.amount,
            "currency": order.currency,
        }
    )
    if warehouse:
        fields = {
            "address": order.address,
            "product_raw": order.product_raw,
            "quantity": order.quantity,
        }
    return OrderOut(
        public_id=order.public_id,
        created_at=order.created_at,
        delivery_status=order.delivery_status,
        delivery_attempts=order.delivery_attempts,
        delivery_error=(None if warehouse or not order.delivery_error else ("delivery_failed" if seller else order.delivery_error)),
        **cast(Any, fields),
    )


@app.exception_handler(ValueError)
async def value_error(_: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(IdempotencyConflict)
async def idempotency_conflict(_: Request, exc: IdempotencyConflict) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(PermissionError)
async def permission_error(_: Request, exc: PermissionError) -> JSONResponse:
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.post("/api/v1/auth/token", response_model=Token)
async def token(form: Annotated[OAuth2PasswordRequestForm, Depends()]) -> Token:
    async with SessionFactory() as s:
        user = await s.scalar(select(User).where(User.username == form.username))
        try:
            valid = user is not None and await verify_password_async(form.password, user.password_hash)
        except (ValueError, TypeError, UnknownHashError):
            valid = False
        if user is None or not valid or not user.is_active:
            raise HTTPException(status_code=401, detail="incorrect credentials")
        return Token(access_token=make_token(user), token_type="bearer")


@app.post("/api/v1/orders", response_model=OrderOut, response_model_exclude_none=True)
async def create_order(
    payload: OrderIn,
    actor: Actor = Depends(require("owner", "manager", "seller")),  # noqa: B008
) -> OrderOut:
    from order_bot.validation import normalize_phone, normalize_product

    async with SessionFactory() as s:
        result = await OrderService(s).create_order(
            CreateOrderCommand(
                payload.customer_name,
                payload.phone_raw,
                normalize_phone(payload.phone_raw),
                payload.province,
                payload.city,
                payload.address,
                payload.postal_code,
                payload.product_raw,
                normalize_product(payload.product_raw),
                payload.quantity,
                payload.amount,
                payload.notes,
                payload.photo_file_id,
                payload.idempotency_key,
                payload.allow_duplicate,
            ),
            actor,
        )
        if result.duplicate_confirmation_required:
            raise HTTPException(409, "duplicate confirmation required")
        if result.order is None:
            raise HTTPException(500, "order creation returned no order")
        return output(result.order, actor)


@app.post("/api/v1/commerce/orders", response_model=OrderOut, response_model_exclude_none=True)
async def create_cart_order(
    payload: CartOrderIn,
    actor: Actor = Depends(require("owner", "manager", "seller")),  # noqa: B008
) -> OrderOut:
    async with SessionFactory() as s:
        result = await OrderService(s).create_cart_order(
            CreateCartOrderCommand(
                payload.customer_name,
                payload.phone_raw,
                payload.province,
                payload.city,
                payload.address,
                payload.postal_code,
                tuple(CartLine(x.sku, x.quantity) for x in payload.items),
                payload.idempotency_key,
                payload.notes,
            ),
            actor,
        )
        if result.order is None:
            raise HTTPException(500, "order creation returned no order")
        return output(result.order, actor)


@app.post("/api/v1/products", status_code=201)
async def create_product(
    payload: ProductIn,
    actor: Actor = Depends(require("owner", "manager")),  # noqa: B008
) -> dict[str, int | str]:
    if payload.currency != "IRR":
        raise HTTPException(422, "only IRR is supported")
    async with SessionFactory() as s:
        product = Product(sku=payload.sku.strip().upper(), name=payload.name.strip(), unit_price=payload.unit_price, currency=payload.currency, is_active=True)
        s.add(product)
        await s.flush()
        s.add(InventoryBalance(product_id=product.id, on_hand=payload.on_hand, reserved=0))
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            raise HTTPException(409, "SKU already exists")
        return {
            "id": product.id,
            "sku": product.sku,
            "name": product.name,
            "unit_price": product.unit_price,
            "currency": product.currency,
            "on_hand": payload.on_hand,
        }


@app.get("/api/v1/products")
async def list_products(
    limit: int = Query(50, ge=1, le=100),
    cursor: str | None = Query(None, min_length=1, max_length=80),
    actor: Actor = Depends(require("owner", "manager", "seller", "warehouse")),  # noqa: B008
) -> dict[str, list[dict[str, int | str]] | str | None]:
    async with SessionFactory() as s:
        query = select(Product, InventoryBalance).join(InventoryBalance, InventoryBalance.product_id == Product.id).where(Product.is_active.is_(True))
        if cursor:
            query = query.where(Product.sku > cursor.upper())
        rows = (await s.execute(query.order_by(Product.sku).limit(limit))).all()
        return {
            "items": [{"sku": p.sku, "name": p.name, "unit_price": p.unit_price, "currency": p.currency, "available": b.on_hand - b.reserved} for p, b in rows],
            "next_cursor": rows[-1][0].sku if len(rows) == limit else None,
        }


@app.get(
    "/api/v1/orders/{public_id}",
    response_model=OrderOut,
    response_model_exclude_none=True,
)
async def get_order(
    public_id: str,
    actor: Actor = Depends(require("owner", "manager", "seller", "warehouse")),  # noqa: B008
) -> OrderOut:
    async with SessionFactory() as s:
        order = await OrderService(s).get_order(public_id, actor)
        if order is None:
            raise HTTPException(404, "order not found")
        return output(order, actor)


@app.get("/api/v1/orders", response_model=OrderPage, response_model_exclude_none=True)
async def list_orders(
    limit: int = Query(50, ge=1, le=100),
    cursor: int | None = Query(None, ge=1),
    actor: Actor = Depends(require("owner", "manager", "seller", "warehouse")),  # noqa: B008
) -> OrderPage:
    async with SessionFactory() as s:
        orders = await OrderService(s).list_orders(actor, limit=limit, cursor=cursor)
        return OrderPage(
            items=[output(o, actor) for o in orders],
            next_cursor=orders[-1].id if len(orders) == limit else None,
        )


@app.post("/api/v1/admins", status_code=201)
async def add_admin(payload: AdminIn, actor: Actor = Depends(require("owner"))) -> dict[str, Any]:  # noqa: B008
    async with SessionFactory() as s:
        admin = Admin(
            telegram_id=payload.telegram_id,
            name=payload.name,
            admin_code=payload.admin_code.upper(),
            is_active=True,
            created_at=datetime.now(UTC),
        )
        s.add(admin)
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            raise HTTPException(409, "admin already exists")
    return {
        "telegram_id": admin.telegram_id,
        "name": admin.name,
        "admin_code": admin.admin_code,
        "is_active": True,
    }


@app.post("/api/v1/users", status_code=201)
async def add_user(payload: UserIn, actor: Actor = Depends(require("owner"))) -> dict[str, Any]:  # noqa: B008
    if payload.role not in {"owner", "manager", "seller", "warehouse"}:
        raise HTTPException(422, "invalid role")
    async with SessionFactory() as s:
        user = User(
            username=payload.username,
            password_hash=await hash_password_async(payload.password),
            role=payload.role,
            telegram_id=payload.telegram_id,
            is_active=True,
            token_version=0,
        )
        s.add(user)
        try:
            await s.commit()
        except IntegrityError:
            await s.rollback()
            raise HTTPException(409, "user already exists")
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": True,
    }


@app.patch("/api/v1/users/{user_id}/revoke")
async def revoke_user(user_id: int, actor: Actor = Depends(require("owner"))) -> dict[str, Any]:  # noqa: B008
    async with SessionFactory() as s:
        user = await s.get(User, user_id)
        if user is None:
            raise HTTPException(404, "user not found")
        user.is_active = False
        user.token_version += 1
        await s.commit()
    return {"id": user_id, "is_active": False}


@app.patch("/api/v1/admins/{telegram_id}")
async def set_admin(
    telegram_id: int,
    payload: ActiveIn,
    actor: Actor = Depends(require("owner")),  # noqa: B008
) -> dict[str, Any]:
    async with SessionFactory() as s:
        admin = await s.get(Admin, telegram_id)
        if admin is None:
            raise HTTPException(404, "admin not found")
        admin.is_active = payload.active
        user = await s.scalar(select(User).where(User.telegram_id == telegram_id))
        if user and not payload.active:
            user.is_active = False
            user.token_version += 1
        await s.commit()
    return {"telegram_id": telegram_id, "is_active": payload.active}


@app.get("/api/v1/users")
async def list_users(
    limit: int = Query(50, ge=1, le=100),
    cursor: int | None = Query(None, ge=1),
    actor: Actor = Depends(require("owner")),  # noqa: B008
) -> list[dict[str, Any]]:
    async with SessionFactory() as s:
        query = select(User).order_by(User.id).limit(limit)
        if cursor is not None:
            query = query.where(User.id > cursor)
        users = (await s.scalars(query)).all()
        return [
            {
                "id": u.id,
                "username": u.username,
                "role": u.role,
                "telegram_id": u.telegram_id,
                "is_active": u.is_active,
            }
            for u in users
        ]


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def ready() -> dict[str, str]:
    try:
        async with SessionFactory() as s:
            await s.execute(select(1))
    except (OSError, SQLAlchemyError):
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# Keep this manifest synchronized with the actual protected/public HTTP surface.
def _route_key(route: APIRoute, method: str) -> str:
    return f"{method} {route.path}"


_actual_route_keys = {
    _route_key(route, method)
    for route in app.routes
    if isinstance(route, APIRoute) and (route.path.startswith("/api/v1/") or route.path.startswith("/health/"))
    for method in getattr(route, "methods", set())
    if method not in {"HEAD", "OPTIONS"}
}
if _actual_route_keys != set(ROUTE_PERMISSIONS):
    raise RuntimeError(f"route permission manifest mismatch: {_actual_route_keys ^ set(ROUTE_PERMISSIONS)}")
