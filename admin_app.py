"""Web admin panel for managing the PostgreSQL FAQ knowledge base."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from urllib.parse import parse_qsl
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from telegram import Bot

from faq_store import FAQStore
from support_store import SupportStore

load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_LOGIN = os.getenv("ADMIN_LOGIN", "admin")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET") or secrets.token_urlsafe(32)
SESSION_TTL_SECONDS = 8 * 60 * 60
COOKIE_SECURE_SETTING = os.getenv("ADMIN_COOKIE_SECURE")
CSRF_COOKIE_NAME = "admin_csrf"
SESSION_COOKIE_NAME = "admin_session"
LOGIN_WINDOW_SECONDS = 15 * 60
LOGIN_MAX_ATTEMPTS = 5
login_attempts: dict[str, list[float]] = {}
LOGGER = logging.getLogger(__name__)
if not DATABASE_URL:
    raise RuntimeError("Укажите DATABASE_URL для PostgreSQL")
if not ADMIN_PASSWORD:
    raise RuntimeError("Укажите ADMIN_PASSWORD для админ-панели")

store = FAQStore(DATABASE_URL)
request_store = SupportStore(DATABASE_URL)
store.init_schema()
store.seed_defaults()
request_store.init_schema()
app = FastAPI(title="FAQ Knowledge Base Admin")
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def cookie_secure(request: Request) -> bool:
    if COOKIE_SECURE_SETTING is not None:
        return COOKIE_SECURE_SETTING.lower() in {"1", "true", "yes", "on"}
    client_host = request.client.host if request.client else ""
    return not (request.url.scheme == "http" and client_host in {"127.0.0.1", "::1", "localhost"})


@app.middleware("http")
async def security_middleware(request: Request, call_next):
    csrf_token = request.cookies.get(CSRF_COOKIE_NAME) or secrets.token_urlsafe(32)
    request.state.csrf_token = csrf_token
    response = await call_next(request)
    if CSRF_COOKIE_NAME not in request.cookies:
        response.set_cookie(CSRF_COOKIE_NAME, csrf_token, httponly=False, secure=cookie_secure(request), samesite="lax", max_age=SESSION_TTL_SECONDS)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self'; script-src 'self' https://telegram.org; style-src 'self' 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
    if request.url.path.startswith("/admin"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return f"{salt.hex()}${digest.hex()}"


def password_matches(password: str, encoded: str) -> bool:
    try:
        salt_hex, digest_hex = encoded.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 240_000).hex()
        return hmac.compare_digest(digest, digest_hex)
    except (ValueError, TypeError):
        return False


if not request_store.get_employee_by_login(ADMIN_LOGIN):
    request_store.create_employee("Главный", "Администратор", "Администратор системы", "admin", ADMIN_LOGIN, password_hash(ADMIN_PASSWORD))


def session_value(employee_id: int) -> str:
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{employee_id}.{expires_at}"
    signature = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def current_employee(request: Request):
    raw = request.cookies.get(SESSION_COOKIE_NAME, "")
    try:
        employee_id, expires_at, signature = raw.split(".", 2)
        payload = f"{employee_id}.{expires_at}"
        expected = hmac.new(SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if int(expires_at) < int(time.time()):
            return None
        employee = request_store.get_employee(int(employee_id))
    except (ValueError, TypeError, OverflowError):
        employee = None
    request.state.employee = employee
    return employee


def authenticated(request: Request) -> bool:
    return current_employee(request) is not None


def role_is(request: Request, *roles: str) -> bool:
    employee = current_employee(request)
    return bool(employee and employee.role in roles)


def csrf_is_valid(request: Request, token: str) -> bool:
    expected = request.cookies.get(CSRF_COOKIE_NAME, "")
    return bool(expected and token and hmac.compare_digest(token, expected))


def csrf_forbidden(request: Request, token: str) -> HTMLResponse | None:
    if not csrf_is_valid(request, token):
        return HTMLResponse("Недействительный CSRF-токен", status_code=403)
    return None


def login_key(request: Request, login_name: str) -> str:
    client_host = request.client.host if request.client else "unknown"
    return f"{client_host}:{login_name.strip().casefold()}"


def login_rate_limited(request: Request, login_name: str) -> bool:
    now = time.time()
    key = login_key(request, login_name)
    attempts = [stamp for stamp in login_attempts.get(key, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    login_attempts[key] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS


def record_login_failure(request: Request, login_name: str) -> None:
    login_attempts.setdefault(login_key(request, login_name), []).append(time.time())


def clear_login_failures(request: Request, login_name: str) -> None:
    login_attempts.pop(login_key(request, login_name), None)


def forbidden() -> HTMLResponse:
    return HTMLResponse("Доступ запрещён", status_code=403)


def login_redirect() -> RedirectResponse:
    return RedirectResponse("/admin/login", status_code=303)


def telegram_webapp_user_id(init_data: str) -> int | None:
    if not TELEGRAM_BOT_TOKEN or not init_data:
        return None
    try:
        fields = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = fields.pop("hash")
        auth_date = int(fields["auth_date"])
        now = int(time.time())
        if auth_date > now + 300 or now - auth_date > 86400:
            return None
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret_key = hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(received_hash, expected_hash):
            return None
        user = json.loads(fields["user"])
        telegram_id = user.get("id")
        return telegram_id if isinstance(telegram_id, int) and telegram_id > 0 else None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse("/admin/faq" if authenticated(request) else "/admin/login", status_code=303)


@app.get("/admin/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if authenticated(request):
        return RedirectResponse("/admin/faq", status_code=303)
    return templates.TemplateResponse(request, "admin.html", {"request": request, "page": "login", "error": None})


@app.get("/admin/miniapp", response_class=HTMLResponse)
async def miniapp_page(request: Request):
    return templates.TemplateResponse(request, "miniapp.html", {"request": request})


@app.post("/admin/miniapp/auth")
async def miniapp_auth(request: Request, init_data: str = Form(...)):
    telegram_id = telegram_webapp_user_id(init_data)
    employee = request_store.get_employee_by_telegram_id(telegram_id) if telegram_id else None
    if not employee or request_store.is_blocked("user", telegram_id):
        return JSONResponse({"detail": "Сотрудник не найден или доступ заблокирован"}, status_code=403)
    response = JSONResponse({"ok": True, "role": employee.role})
    response.set_cookie(SESSION_COOKIE_NAME, session_value(employee.id), httponly=True, samesite="lax", secure=cookie_secure(request), max_age=SESSION_TTL_SECONDS)
    return response


@app.post("/admin/login", response_class=HTMLResponse)
async def login(request: Request, login_name: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    if login_rate_limited(request, login_name):
        return HTMLResponse("Слишком много попыток входа. Повторите позже.", status_code=429)
    employee = request_store.get_employee_by_login(login_name.strip())
    if not employee or not password_matches(password, employee.password_hash):
        record_login_failure(request, login_name)
        return templates.TemplateResponse(
            request, "admin.html", {"request": request, "page": "login", "error": "Неверный логин или пароль"}, status_code=401
        )
    clear_login_failures(request, login_name)
    response = RedirectResponse("/admin/requests" if employee.role == "operator" else "/admin/faq", status_code=303)
    response.set_cookie(SESSION_COOKIE_NAME, session_value(employee.id), httponly=True, samesite="lax", secure=cookie_secure(request), max_age=SESSION_TTL_SECONDS)
    return response


@app.post("/admin/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/admin/faq", response_class=HTMLResponse)
async def faq_list(request: Request, q: str = ""):
    if not authenticated(request):
        return login_redirect()
    if role_is(request, "operator"):
        return RedirectResponse("/admin/requests", status_code=303)
    return templates.TemplateResponse(
        request, "admin.html", {"request": request, "page": "list", "items": store.list(q), "query": q}
    )


@app.get("/admin/faq/new", response_class=HTMLResponse)
async def faq_new(request: Request):
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    return templates.TemplateResponse(request, "admin.html", {"request": request, "page": "form", "item": None, "error": None})


@app.post("/admin/faq/new")
async def faq_create(
    request: Request,
    csrf_token: str = Form(...),
    question: str = Form(...),
    answer: str = Form(...),
    is_active: bool = Form(False),
):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    if not question.strip() or not answer.strip():
        return templates.TemplateResponse(
            request, "admin.html", {"request": request, "page": "form", "item": None, "error": "Заполните оба поля"}, status_code=400
        )
    try:
        store.create(question, answer, is_active)
    except Exception:
        LOGGER.exception("Не удалось сохранить FAQ")
        return templates.TemplateResponse(
            request, "admin.html", {"request": request, "page": "form", "item": None, "error": "Не удалось сохранить FAQ"}, status_code=400
        )
    return RedirectResponse("/admin/faq", status_code=303)


@app.get("/admin/faq/{item_id}/edit", response_class=HTMLResponse)
async def faq_edit(request: Request, item_id: int):
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    item = store.get(item_id)
    if not item:
        return RedirectResponse("/admin/faq", status_code=303)
    return templates.TemplateResponse(request, "admin.html", {"request": request, "page": "form", "item": item, "error": None})


@app.post("/admin/faq/{item_id}/edit")
async def faq_update(
    request: Request,
    item_id: int,
    csrf_token: str = Form(...),
    question: str = Form(...),
    answer: str = Form(...),
    is_active: bool = Form(False),
):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    if not question.strip() or not answer.strip():
        item = store.get(item_id)
        return templates.TemplateResponse(
            request, "admin.html", {"request": request, "page": "form", "item": item, "error": "Заполните оба поля"}, status_code=400
        )
    store.update(item_id, question, answer, is_active)
    return RedirectResponse("/admin/faq", status_code=303)


@app.post("/admin/faq/{item_id}/delete")
async def faq_delete(request: Request, item_id: int, csrf_token: str = Form(...)):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    store.delete(item_id)
    return RedirectResponse("/admin/faq", status_code=303)


@app.get("/admin/requests", response_class=HTMLResponse)
async def request_list(request: Request, q: str = "", status: str = "", kind: str | None = None):
    if not authenticated(request):
        return login_redirect()
    if kind is None:
        kind = "manual"
    if kind not in {"", "manual", "auto"}:
        kind = "manual"
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"request": request, "page": "requests", "items": request_store.list(q, status, kind), "query": q, "status": status, "kind": kind},
    )


@app.get("/admin/requests/{request_id}", response_class=HTMLResponse)
async def request_detail(request: Request, request_id: int):
    if not authenticated(request):
        return login_redirect()
    item = request_store.get(request_id)
    if not item:
        return RedirectResponse("/admin/requests", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"request": request, "page": "request_detail", "item": item, "messages": request_store.messages(request_id), "error": None},
    )


@app.post("/admin/requests/{request_id}/reply")
async def request_reply(request: Request, request_id: int, csrf_token: str = Form(...), body: str = Form(...)):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin", "operator"):
        return forbidden()
    employee = current_employee(request)
    item = request_store.get(request_id)
    if not item:
        return RedirectResponse("/admin/requests", status_code=303)
    if not body.strip():
        return templates.TemplateResponse(
            request,
            "admin.html",
            {"request": request, "page": "request_detail", "item": item, "messages": request_store.messages(request_id), "error": "Введите текст ответа"},
            status_code=400,
        )
    if not TELEGRAM_BOT_TOKEN:
        return templates.TemplateResponse(
            request,
            "admin.html",
            {"request": request, "page": "request_detail", "item": item, "messages": request_store.messages(request_id), "error": "TELEGRAM_BOT_TOKEN не настроен"},
            status_code=500,
        )
    request_store.add_message(request_id, employee.id, "employee", body.strip())
    request_store.set_status(request_id, "answered")
    try:
        async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
            await bot.send_message(chat_id=item.chat_id, text=f"Ответ сотрудника по заявке №{item.id}:\n{body.strip()}")
    except Exception:
        LOGGER.exception("Не удалось отправить ответ в Telegram")
        return templates.TemplateResponse(
            request,
            "admin.html",
            {"request": request, "page": "request_detail", "item": item, "messages": request_store.messages(request_id), "error": "Ответ сохранён, но не отправлен в Telegram"},
            status_code=502,
        )
    return RedirectResponse(f"/admin/requests/{request_id}", status_code=303)


@app.post("/admin/requests/{request_id}/close")
async def request_close(request: Request, request_id: int, csrf_token: str = Form(...)):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    request_store.set_status(request_id, "closed")
    return RedirectResponse(f"/admin/requests/{request_id}", status_code=303)


@app.get("/admin/users-groups", response_class=HTMLResponse)
async def users_groups(request: Request):
    if not authenticated(request):
        return login_redirect()
    if role_is(request, "operator"):
        return RedirectResponse("/admin/requests", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "request": request,
            "page": "users_groups",
            "users": request_store.list_entities("user"),
            "groups": request_store.list_entities("group"),
        },
    )


@app.post("/admin/entities/manual-block")
async def manual_block(
    request: Request,
    csrf_token: str = Form(...),
    entity_type: str = Form(...),
    entity_id: int = Form(...),
    title: str = Form(""),
):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    if entity_type not in {"user", "group"}:
        return RedirectResponse("/admin/users-groups", status_code=303)
    request_store.register_entity(entity_type, entity_id, title.strip() or str(entity_id))
    request_store.set_entity_blocked(entity_type, entity_id, True)
    return RedirectResponse("/admin/users-groups", status_code=303)


@app.post("/admin/entities/{entity_type}/{entity_id}/block")
async def block_entity(request: Request, entity_type: str, entity_id: int, csrf_token: str = Form(...)):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    if not authenticated(request) or entity_type not in {"user", "group"}:
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    request_store.set_entity_blocked(entity_type, entity_id, True)
    return RedirectResponse("/admin/users-groups", status_code=303)


@app.post("/admin/entities/{entity_type}/{entity_id}/unblock")
async def unblock_entity(request: Request, entity_type: str, entity_id: int, csrf_token: str = Form(...)):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    if not authenticated(request) or entity_type not in {"user", "group"}:
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    request_store.set_entity_blocked(entity_type, entity_id, False)
    return RedirectResponse("/admin/users-groups", status_code=303)


VALID_ROLES = {"admin", "analyst", "operator"}


@app.get("/admin/employees", response_class=HTMLResponse)
async def employee_list(request: Request):
    if not authenticated(request):
        return login_redirect()
    if role_is(request, "operator"):
        return RedirectResponse("/admin/requests", status_code=303)
    return templates.TemplateResponse(
        request,
        "admin.html",
        {"request": request, "page": "employees", "employees": request_store.list_employees(), "error": None},
    )


@app.get("/admin/employees/new", response_class=HTMLResponse)
async def employee_new(request: Request):
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    return templates.TemplateResponse(request, "admin.html", {"request": request, "page": "employee_form", "item": None, "error": None})


@app.post("/admin/employees/new")
async def employee_create(
    request: Request,
    csrf_token: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    position: str = Form(...),
    role: str = Form(...),
    login_name: str = Form(...),
    password: str = Form(...),
    telegram_id: str = Form(""),
):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    values = [first_name.strip(), last_name.strip(), position.strip(), login_name.strip()]
    telegram_id_value = int(telegram_id.strip()) if telegram_id.strip().isdigit() else None
    if not all(values) or role not in VALID_ROLES or len(password) < 6 or (telegram_id.strip() and telegram_id_value is None):
        return templates.TemplateResponse(request, "admin.html", {"request": request, "page": "employee_form", "item": None, "error": "Заполните поля, выберите роль и задайте пароль не короче 6 символов"}, status_code=400)
    try:
        request_store.create_employee(*values[:3], role, values[3], password_hash(password), telegram_id_value)
    except Exception:
        LOGGER.exception("Не удалось создать сотрудника")
        return templates.TemplateResponse(request, "admin.html", {"request": request, "page": "employee_form", "item": None, "error": "Не удалось создать сотрудника: логин уже может быть занят"}, status_code=400)
    return RedirectResponse("/admin/employees", status_code=303)


@app.get("/admin/employees/{employee_id}/edit", response_class=HTMLResponse)
async def employee_edit(request: Request, employee_id: int):
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    item = request_store.get_employee_public(employee_id)
    if not item:
        return RedirectResponse("/admin/employees", status_code=303)
    return templates.TemplateResponse(request, "admin.html", {"request": request, "page": "employee_form", "item": item, "error": None})


@app.post("/admin/employees/{employee_id}/edit")
async def employee_update(
    request: Request,
    employee_id: int,
    csrf_token: str = Form(...),
    first_name: str = Form(...),
    last_name: str = Form(...),
    position: str = Form(...),
    role: str = Form(...),
    login_name: str = Form(...),
    password: str = Form(""),
    telegram_id: str = Form(""),
):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    values = [first_name.strip(), last_name.strip(), position.strip(), login_name.strip()]
    telegram_id_value = int(telegram_id.strip()) if telegram_id.strip().isdigit() else None
    item = request_store.get_employee_public(employee_id)
    if not item:
        return RedirectResponse("/admin/employees", status_code=303)
    if not all(values) or role not in VALID_ROLES or (password and len(password) < 6) or (telegram_id.strip() and telegram_id_value is None):
        return templates.TemplateResponse(request, "admin.html", {"request": request, "page": "employee_form", "item": item, "error": "Проверьте заполнение полей и длину нового пароля"}, status_code=400)
    try:
        request_store.update_employee(employee_id, *values[:3], role, values[3], password_hash(password) if password else None, telegram_id_value)
    except Exception:
        LOGGER.exception("Не удалось обновить сотрудника")
        return templates.TemplateResponse(request, "admin.html", {"request": request, "page": "employee_form", "item": item, "error": "Не удалось сохранить: логин уже может быть занят"}, status_code=400)
    return RedirectResponse("/admin/employees", status_code=303)


@app.post("/admin/employees/{employee_id}/delete")
async def employee_delete(request: Request, employee_id: int, csrf_token: str = Form(...)):
    csrf_error = csrf_forbidden(request, csrf_token)
    if csrf_error:
        return csrf_error
    if not authenticated(request):
        return login_redirect()
    if not role_is(request, "admin"):
        return forbidden()
    employee = current_employee(request)
    if employee and employee.id == employee_id:
        return HTMLResponse("Нельзя удалить собственную учётную запись", status_code=400)
    request_store.delete_employee(employee_id)
    return RedirectResponse("/admin/employees", status_code=303)
