# pylint: disable=no-member, no-name-in-module
import json
import re
import secrets
from datetime import timedelta
from decimal import Decimal
from typing import List, Optional

import asyncpg
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Security,
    UploadFile,
)
from fastapi.security import SecurityScopes
from pydantic.error_wrappers import ValidationError
from sqlalchemy import distinct, func, select
from starlette.endpoints import WebSocketEndpoint
from starlette.requests import Request
from starlette.status import WS_1008_POLICY_VIOLATION

from . import crud, db, models, pagination, schemes, settings, tasks, utils

router = APIRouter()


def get_user():
    return models.User


def get_wallet():
    return models.User.join(models.Wallet)


def get_store():
    return models.Store.join(models.WalletxStore).join(models.Wallet).join(models.User)


def get_product():
    return (
        models.Product.join(models.Store)
        .join(models.WalletxStore)
        .join(models.Wallet)
        .join(models.User)
    )


def get_discount():
    return models.Discount.join(models.User)


def get_invoice():
    return (
        models.Invoice.join(models.Store)
        .join(models.WalletxStore)
        .join(models.Wallet)
        .join(models.User)
    )


@router.get("/users/me", response_model=schemes.DisplayUser)
async def get_me(user: models.User = Security(utils.AuthDependency())):
    return user


@router.get("/wallets/balance", response_model=Decimal)
async def get_balances(
    user: models.User = Security(utils.AuthDependency(), scopes=["wallet_management"])
):
    balances = Decimal()
    async with db.db.acquire() as conn:
        async with conn.transaction():
            async for wallet in models.Wallet.query.select_from(get_wallet()).where(
                models.User.id == user.id
            ).gino.iterate():
                balance = (
                    await settings.get_coin(wallet.currency, wallet.xpub).balance()
                )["confirmed"]
                balances += Decimal(balance)
    return balances


@router.get("/stores/{model_id}/ping")
async def ping_email(
    model_id: int,
    user: models.User = Security(utils.AuthDependency(), scopes=["store_management"]),
):
    model = (
        await models.Store.query.select_from(get_store())
        .where(models.Store.id == model_id)
        .gino.first()
    )
    if not model:
        raise HTTPException(404, f"Store with id {model_id} does not exist!")
    return utils.check_ping(
        model.email_host,
        model.email_port,
        model.email_user,
        model.email_password,
        model.email,
        model.email_use_ssl,
    )


# invoices and products should have unauthorized access
async def get_product_noauth(model_id: int):
    item = await models.Product.get(model_id)
    if not item:
        raise HTTPException(
            status_code=404, detail=f"Object with id {model_id} does not exist!"
        )
    await crud.product_add_related(item)
    return item


async def get_invoice_noauth(model_id: int):
    item = await models.Invoice.get(model_id)
    if not item:
        raise HTTPException(
            status_code=404, detail=f"Object with id {model_id} does not exist!"
        )
    await crud.invoice_add_related(item)
    return item


async def get_products(
    request: Request,
    pagination: pagination.Pagination = Depends(),
    store: Optional[int] = None,
    category: Optional[str] = "",
    min_price: Optional[Decimal] = None,
    max_price: Optional[Decimal] = None,
    sale: Optional[bool] = False,
):
    try:
        user = await utils.AuthDependency()(
            request, SecurityScopes(["product_management"])
        )
    except HTTPException:
        if store is None:
            raise
        user = None
    return await pagination.paginate(
        models.Product,
        get_product(),
        user.id if user else None,
        store,
        category,
        min_price,
        max_price,
        sale,
        postprocess=crud.products_add_related,
    )


async def create_product(
    data: str = Form(...),
    image: UploadFile = File(None),
    user: models.User = Security(utils.AuthDependency(), scopes=["product_management"]),
):
    filename = utils.get_image_filename(image)
    data = json.loads(data)
    try:
        data = schemes.CreateProduct(**data)
    except ValidationError as e:
        raise HTTPException(422, e.errors())
    data.image = filename
    d = data.dict()
    discounts = d.pop("discounts", None)
    try:
        obj = await models.Product.create(**d)
        created = []
        for i in discounts:
            created.append(
                (
                    await models.DiscountxProduct.create(
                        product_id=obj.id, discount_id=i
                    )
                ).discount_id
            )
        obj.discounts = created
        if image:
            filename = utils.get_image_filename(image, False, obj)
            await obj.update(image=filename).apply()
            await utils.save_image(filename, image)
    except (
        asyncpg.exceptions.UniqueViolationError,
        asyncpg.exceptions.NotNullViolationError,
        asyncpg.exceptions.ForeignKeyViolationError,
    ) as e:
        raise HTTPException(422, e.message)
    return obj


async def process_edit_product(model_id, data, image, user, patch=True):
    data = json.loads(data)
    try:
        model = schemes.Product(**data)
    except ValidationError as e:
        raise HTTPException(422, e.errors())
    item = await get_product_noauth(model_id)
    if image:
        filename = utils.get_image_filename(image, False, item)
        model.image = filename
        await utils.save_image(filename, image)
    else:
        utils.safe_remove(item.image)
        model.image = None
    try:
        if patch:
            await item.update(
                **model.dict(exclude_unset=True)  # type: ignore
            ).apply()
        else:
            await item.update(**model.dict()).apply()
    except (  # pragma: no cover
        asyncpg.exceptions.UniqueViolationError,
        asyncpg.exceptions.NotNullViolationError,
        asyncpg.exceptions.ForeignKeyViolationError,
    ) as e:
        raise HTTPException(422, e.message)  # pragma: no cover
    return item


async def patch_product(
    model_id: int,
    data: str = Form(...),
    image: UploadFile = File(None),
    user: models.User = Security(utils.AuthDependency(), scopes=["product_management"]),
):
    return await process_edit_product(model_id, data, image, user)


async def put_product(
    model_id: int,
    data: str = Form(...),
    image: UploadFile = File(None),
    user: models.User = Security(utils.AuthDependency(), scopes=["product_management"]),
):
    return await process_edit_product(model_id, data, image, user, patch=False)


async def delete_product(item: schemes.Product, user: schemes.User) -> schemes.Product:
    await crud.product_add_related(item)
    utils.safe_remove(item.image)
    await item.delete()
    return item


async def products_count(
    request: Request,
    store: Optional[int] = None,
    category: Optional[str] = "",
    min_price: Optional[Decimal] = None,
    max_price: Optional[Decimal] = None,
    sale: Optional[bool] = False,
):
    query = models.Product.query
    if sale:
        query = (
            query.select_from(
                get_product().join(models.DiscountxProduct).join(models.Discount)
            )
            .having(func.count(models.DiscountxProduct.product_id) > 0)
            .where(models.Discount.end_date > utils.now())
        )
    if store is None:
        user = await utils.AuthDependency()(
            request, SecurityScopes(["product_management"])
        )
        if not sale:
            query = query.select_from(get_product())
        query = query.where(models.User.id == user.id)
    else:
        query = query.where(models.Product.store_id == store)
    if category and category != "all":
        query = query.where(models.Product.category == category)
    if min_price is not None:
        query = query.where(models.Product.price >= min_price)
    if max_price is not None:
        query = query.where(models.Product.price <= max_price)
    return (
        await (
            query.with_only_columns([db.db.func.count(distinct(models.Product.id))])
            .order_by(None)
            .gino.scalar()
        )
        or 0
    )


@router.get("/invoices/order_id/{order_id}", response_model=schemes.DisplayInvoice)
async def get_invoice_by_order_id(order_id: str):
    item = await models.Invoice.query.where(
        models.Invoice.order_id == order_id
    ).gino.first()
    if not item:
        raise HTTPException(
            status_code=404, detail=f"Object with order id {order_id} does not exist!"
        )
    await crud.invoice_add_related(item)
    return item


@router.get("/products/maxprice")
async def get_max_product_price(store: int):
    return (
        await (
            models.Product.query.select_from(get_product())
            .where(models.Store.id == store)
            .with_only_columns([db.db.func.max(distinct(models.Product.price))])
            .order_by(None)
            .gino.scalar()
        )
        or 0
    )


@router.get("/fiatlist")
async def get_fiatlist(query: Optional[str] = None):
    s = None
    for coin in settings.cryptos:
        fiat_list = await settings.cryptos[coin].list_fiat()
        if not s:
            s = set(fiat_list)
        else:
            s = s.intersection(fiat_list)
    if query is not None:
        pattern = re.compile(query, re.IGNORECASE)
        s = [x for x in s if pattern.match(x)]
    return sorted(s)


utils.model_view(
    router,
    "/users",
    models.User,
    schemes.User,
    get_user,
    schemes.CreateUser,
    display_model=schemes.DisplayUser,
    custom_methods={
        "post": crud.create_user,
        "patch": crud.patch_user,
        "put": crud.put_user,
    },
    post_auth=False,
    scopes={
        "get_all": ["server_management"],
        "get_count": ["server_management"],
        "get_one": ["server_management"],
        "post": [],
        "patch": ["server_management"],
        "put": ["server_management"],
        "delete": ["server_management"],
    },
)
utils.model_view(
    router,
    "/wallets",
    models.Wallet,
    schemes.CreateWallet,
    get_wallet,
    schemes.CreateWallet,
    schemes.Wallet,
    background_tasks_mapping={"post": tasks.sync_wallet},
    custom_methods={"post": crud.create_wallet},
    scopes=["wallet_management"],
)
utils.model_view(
    router,
    "/stores",
    models.Store,
    schemes.Store,
    get_store,
    schemes.CreateStore,
    custom_methods={
        "get": crud.get_stores,
        "get_one": crud.get_store,
        "post": crud.create_store,
        "delete": crud.delete_store,
    },
    get_one_model=None,
    get_one_auth=False,
    scopes=["store_management"],
)
utils.model_view(
    router,
    "/discounts",
    models.Discount,
    schemes.Discount,
    get_discount,
    schemes.CreateDiscount,
    custom_methods={"post": crud.create_discount},
    scopes=["discount_management"],
)
utils.model_view(
    router,
    "/products",
    models.Product,
    schemes.Product,
    get_product,
    schemes.CreateProduct,
    custom_methods={"delete": delete_product},
    request_handlers={
        "get": get_products,
        "get_one": get_product_noauth,
        "post": create_product,
        "patch": patch_product,
        "put": put_product,
        "get_count": products_count,
    },
    scopes=["product_management"],
)
utils.model_view(
    router,
    "/invoices",
    models.Invoice,
    schemes.Invoice,
    get_invoice,
    schemes.CreateInvoice,
    schemes.DisplayInvoice,
    custom_methods={
        "get": crud.get_invoices,
        "get_one": crud.get_invoice,
        "post": crud.create_invoice,
        "delete": crud.delete_invoice,
    },
    request_handlers={"get_one": get_invoice_noauth},
    post_auth=False,
    scopes=["invoice_management"],
)


@router.get("/crud/stats")
async def get_stats(
    user: models.User = Security(utils.AuthDependency(), scopes=["full_control"])
):
    queries = []
    output_formats = []
    for index, (path, orm_model, data_source) in enumerate(utils.crud_models):
        queries.append(
            select([func.count(distinct(orm_model.id))])
            .select_from(data_source())
            .where(models.User.id == user.id)
            .label(path[1:])  # remove / from name
        )
        output_formats.append((path[1:], index))
    result = await db.db.first(select(queries))
    response = {key: result[ind] for key, ind in output_formats}
    response.pop("users", None)
    response["balance"] = await get_balances(user)
    return response


@router.get("/rate")
async def rate(currency: str = "btc"):
    return await settings.get_coin(currency).rate()


@router.get("/categories")
async def categories(store: int):
    return {
        category
        for category, in await models.Product.select("category")
        .where(models.Product.store_id == store)
        .gino.all()
        if category
    }.union({"all"})


@router.get("/wallet_history/{model_id}", response_model=List[schemes.TxResponse])
async def wallet_history(
    model_id: int,
    user: models.User = Security(utils.AuthDependency(), scopes=["wallet_management"]),
):
    response: List[schemes.TxResponse] = []
    if model_id == 0:
        for model in await models.Wallet.query.select_from(get_wallet()).gino.all():
            await utils.get_wallet_history(model, response)
    else:
        model = (
            await models.Wallet.query.select_from(get_wallet())
            .where(models.Wallet.id == model_id)
            .gino.first()
        )
        if not model:
            raise HTTPException(404, f"Wallet with id {model_id} does not exist!")
        await utils.get_wallet_history(model, response)
    return response


@router.get("/token", response_model=utils.get_pagination_model(schemes.Token))
async def get_tokens(
    user: models.User = Security(utils.AuthDependency(), scopes=["token_management"]),
    pagination: pagination.Pagination = Depends(),
    app_id: Optional[str] = None,
    redirect_url: Optional[str] = None,
    permissions: List[str] = Query(None),
):
    return await pagination.paginate(
        models.Token,
        models.User.join(models.Token),
        user.id,
        app_id=app_id,
        redirect_url=redirect_url,
        permissions=permissions,
    )


@router.get("/token/current", response_model=schemes.Token)
async def get_current_token(request: Request):
    _, token = await utils.AuthDependency()(
        request, SecurityScopes(), return_token=True
    )
    return token


@router.get("/token/count", response_model=int)
async def get_token_count(
    user: models.User = Security(utils.AuthDependency(), scopes=["token_management"]),
    pagination: pagination.Pagination = Depends(),
    app_id: Optional[str] = None,
    redirect_url: Optional[str] = None,
    permissions: List[str] = Query(None),
):
    return await pagination.paginate(
        models.Token,
        models.User.join(models.Token),
        user.id,
        app_id=app_id,
        redirect_url=redirect_url,
        permissions=permissions,
        count_only=True,
    )


@router.patch("/token/{model_id}", response_model=schemes.Token)
async def patch_token(
    model_id: str,
    model: schemes.EditToken,
    user: models.User = Security(utils.AuthDependency(), scopes=["token_management"]),
):
    item = (
        await models.Token.query.where(models.Token.user_id == user.id)
        .where(models.Token.id == model_id)
        .gino.first()
    )
    if not item:
        raise HTTPException(
            status_code=404, detail=f"Token with id {model_id} does not exist!"
        )
    try:
        await item.update(**model.dict(exclude_unset=True)).apply()
    except (
        asyncpg.exceptions.UniqueViolationError,
        asyncpg.exceptions.NotNullViolationError,
        asyncpg.exceptions.ForeignKeyViolationError,
    ) as e:
        raise HTTPException(422, e.message)
    return item


@router.delete("/token/{model_id}", response_model=schemes.Token)
async def delete_token(
    model_id: str,
    user: models.User = Security(utils.AuthDependency(), scopes=["token_management"]),
):
    item = (
        await models.Token.query.where(models.Token.user_id == user.id)
        .where(models.Token.id == model_id)
        .gino.first()
    )
    if not item:
        raise HTTPException(
            status_code=404, detail=f"Token with id {model_id} does not exist!"
        )
    await item.delete()
    return item


@router.post("/token")
async def create_token(
    request: Request,
    token_data: Optional[schemes.HTTPCreateLoginToken] = schemes.HTTPCreateLoginToken(),
):
    token = None
    try:
        user, token = await utils.AuthDependency()(
            request, SecurityScopes(), return_token=True
        )
    except HTTPException:
        user, status = await utils.authenticate_user(
            token_data.email, token_data.password
        )
        if not user:
            raise HTTPException(401, {"message": "Unauthorized", "status": status})
    token_data = token_data.dict()
    strict = token_data.pop("strict")
    if "server_management" in token_data["permissions"] and not user.is_superuser:
        if strict:
            raise HTTPException(
                422, "This application requires access to server settings"
            )
        token_data["permissions"].remove("server_management")
    if token and not "full_control" in token.permissions:
        for permission in token_data["permissions"]:
            if permission not in token.permissions:
                raise HTTPException(403, "Not enough permissions")
    token = await models.Token.create(
        **schemes.CreateDBToken(user_id=user.id, **token_data).dict()
    )
    return {
        **schemes.Token.from_orm(token).dict(),
        "access_token": token.id,
        "token_type": "bearer",
    }


@router.post("/manage/update")
async def update_server(
    user: models.User = Security(utils.AuthDependency(), scopes=["server_management"])
):
    if settings.DOCKER_ENV:
        utils.run_host("./update.sh")
        return {"status": "success", "message": "Successfully started update process!"}
    return {"status": "error", "message": "Not running in docker"}


@router.post("/manage/cleanup")
async def cleanup_server(
    user: models.User = Security(utils.AuthDependency(), scopes=["server_management"])
):
    if settings.DOCKER_ENV:
        utils.run_host("./cleanup.sh")
        return {"status": "success", "message": "Successfully started cleanup process!"}
    return {"status": "error", "message": "Not running in docker"}


@router.get("/manage/daemons")
async def get_daemons(
    user: models.User = Security(utils.AuthDependency(), scopes=["server_management"])
):
    return settings.crypto_settings


@router.get("/manage/policies", response_model=schemes.Policy)
async def get_policies():
    return await utils.get_setting(schemes.Policy)


@router.post("/manage/policies", response_model=schemes.Policy)
async def set_policies(
    settings: schemes.Policy,
    user: models.User = Security(utils.AuthDependency(), scopes=["server_management"]),
):
    return await utils.set_setting(settings)


@router.get("/manage/stores", response_model=schemes.GlobalStorePolicy)
async def get_store_policies():
    return await utils.get_setting(schemes.GlobalStorePolicy)


@router.post("/manage/stores", response_model=schemes.GlobalStorePolicy)
async def set_store_policies(
    settings: schemes.GlobalStorePolicy,
    user: models.User = Security(utils.AuthDependency(), scopes=["server_management"]),
):
    return await utils.set_setting(settings)


@router.websocket_route("/ws/wallets/{model_id}")
class WalletNotify(WebSocketEndpoint):
    subscriber = None

    async def on_connect(self, websocket, **kwargs):
        await websocket.accept()
        self.channel_name = secrets.token_urlsafe(32)
        try:
            self.wallet_id = int(websocket.path_params["model_id"])
            self.access_token = websocket.query_params["token"]
        except (ValueError, KeyError):
            await websocket.close(code=WS_1008_POLICY_VIOLATION)
            return
        try:
            self.user = await utils.AuthDependency(token=self.access_token)(
                None, SecurityScopes(["wallet_management"])
            )
        except HTTPException:
            await websocket.close(code=WS_1008_POLICY_VIOLATION)
            return
        self.wallet = (
            await models.Wallet.query.select_from(get_wallet())
            .where(models.Wallet.id == self.wallet_id)
            .gino.first()
        )
        if not self.wallet:
            await websocket.close(code=WS_1008_POLICY_VIOLATION)
            return
        self.subscriber, self.channel = await utils.make_subscriber(self.wallet_id)
        settings.loop.create_task(self.poll_subs(websocket))

    async def poll_subs(self, websocket):
        while await self.channel.wait_message():
            msg = await self.channel.get_json()
            await websocket.send_json(msg)

    async def on_disconnect(self, websocket, close_code):
        if self.subscriber:
            await self.subscriber.unsubscribe(f"channel:{self.wallet_id}")


@router.websocket_route("/ws/invoices/{model_id}")
class InvoiceNotify(WebSocketEndpoint):
    subscriber = None

    async def on_connect(self, websocket, **kwargs):
        await websocket.accept()
        self.channel_name = secrets.token_urlsafe(32)
        try:
            self.invoice_id = int(websocket.path_params["model_id"])
        except (ValueError, KeyError):
            await websocket.close(code=WS_1008_POLICY_VIOLATION)
            return
        self.invoice = (
            await models.Invoice.query.select_from(get_invoice())
            .where(models.Invoice.id == self.invoice_id)
            .gino.first()
        )
        if not self.invoice:
            await websocket.close(code=WS_1008_POLICY_VIOLATION)
            return
        if self.invoice.status != "Pending":
            await websocket.send_json({"status": self.invoice.status})
            await websocket.close()
            return
        self.invoice = await crud.get_invoice(self.invoice_id, None, self.invoice)
        self.subscriber, self.channel = await utils.make_subscriber(self.invoice_id)
        settings.loop.create_task(self.poll_subs(websocket))

    async def poll_subs(self, websocket):
        while await self.channel.wait_message():
            msg = await self.channel.get_json()
            await websocket.send_json(msg)

    async def on_disconnect(self, websocket, close_code):
        if self.subscriber:
            await self.subscriber.unsubscribe(f"channel:{self.invoice_id}")
