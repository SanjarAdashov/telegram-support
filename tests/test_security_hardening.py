from __future__ import annotations

import ast
from pathlib import Path
import re


ROOT = Path(__file__).parents[1]
ADMIN_APP = ROOT / "admin_app.py"
TEMPLATE = ROOT / "templates" / "admin.html"
SUPPORT_STORE = ROOT / "support_store.py"


def _post_handlers() -> list[ast.AsyncFunctionDef]:
    tree = ast.parse(ADMIN_APP.read_text(encoding="utf-8"))
    handlers = []
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "post"
            for decorator in node.decorator_list
        ):
            handlers.append(node)
    return handlers


def test_every_admin_post_handler_requires_csrf_and_validates_it():
    handlers = _post_handlers()
    assert handlers
    for handler in handlers:
        if handler.name == "miniapp_auth":
            arguments = {argument.arg for argument in handler.args.args}
            assert "init_data" in arguments
            continue
        arguments = {argument.arg for argument in handler.args.args}
        assert "csrf_token" in arguments, handler.name
        source = ast.get_source_segment(ADMIN_APP.read_text(encoding="utf-8"), handler)
        assert source and "csrf_forbidden(request, csrf_token)" in source, handler.name


def test_every_post_form_contains_csrf_input():
    html = TEMPLATE.read_text(encoding="utf-8")
    forms = re.findall(r'<form[^>]*method="post"[^>]*>(.*?)</form>', html, flags=re.DOTALL)
    assert forms
    assert all("csrf_input()" in form for form in forms)


def test_employee_listing_does_not_select_password_hash():
    source = SUPPORT_STORE.read_text(encoding="utf-8")
    list_start = source.index("def list_employees")
    list_end = source.index("def get_employee_public", list_start)
    listing = source[list_start:list_end]
    assert "password_hash" not in listing


def test_sensitive_http_logging_is_disabled_and_session_cookie_is_not_explicitly_insecure():
    bot_source = (ROOT / "support_bot.py").read_text(encoding="utf-8")
    admin_source = ADMIN_APP.read_text(encoding="utf-8")
    assert 'logging.getLogger("httpx").setLevel(logging.WARNING)' in bot_source
    assert 'logging.getLogger("httpcore").setLevel(logging.WARNING)' in bot_source
    assert 'secure=False' not in admin_source
    assert "ADMIN_SESSION_SECRET" in admin_source


def test_miniapp_uses_signed_telegram_data_and_employee_lookup():
    bot_source = (ROOT / "support_bot.py").read_text(encoding="utf-8")
    admin_source = ADMIN_APP.read_text(encoding="utf-8")
    miniapp_source = (ROOT / "templates" / "miniapp.html").read_text(encoding="utf-8")
    script_source = (ROOT / "static" / "miniapp.js").read_text(encoding="utf-8")
    assert "get_employee_by_telegram_id" in bot_source
    assert "WebAppInfo" in bot_source
    assert 'hmac.compare_digest(received_hash, expected_hash)' in admin_source
    assert 'hmac.new(b"WebAppData"' in admin_source
    assert "get_employee_by_telegram_id(telegram_id)" in admin_source
    assert "webApp.initData" in script_source
    assert "miniapp.js" in miniapp_source
