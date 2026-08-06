from __future__ import annotations

import hmac
import json
import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field, field_validator

from app.auth import SessionSigner, UserStore
from app.cloud import CloudClient, capacity_estimate, capacity_status_label
from app.health import AccountHealthService
from app.manager import RegistrationManager
from app.monitor import CloudRegistrationMonitor
from app.notifications import BarkStockNotifier
from app.registration.outlook import OutlookMailboxPool
from app.storage import DEFAULT_SETTINGS, JsonStore, deep_merge
from app.time_utils import iso_now, today as china_today


ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = ROOT / "web"
COOKIE_NAME = "reg_session"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=256)


class PasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class RegistrationSettings(BaseModel):
    count: int = Field(default=1, ge=1, le=100)
    concurrency: int = Field(default=1, ge=1, le=50)
    providers: dict[str, dict[str, int]] = Field(default_factory=dict)
    channel: str = "protocol"
    proxy: str = Field(default="", max_length=500)
    browser_profile: str = "chrome_windows"
    browser_engine: str = "camoufox"
    browser_headless: bool = False
    browser_slow_mo_ms: int = Field(default=40, ge=0, le=1000)
    request_timeout: float = Field(default=45, ge=10, le=300)
    mail_wait_timeout: float = Field(default=120, ge=30, le=600)
    mail_poll_interval: float = Field(default=3, ge=0.5, le=30)

    @field_validator("browser_profile")
    @classmethod
    def validate_profile(cls, value: str) -> str:
        if value not in {"chrome_windows", "safari_macos"}:
            raise ValueError("browser_profile 仅支持 chrome_windows 或 safari_macos")
        return value

    @field_validator("browser_engine")
    @classmethod
    def validate_browser_engine(cls, value: str) -> str:
        source = value.strip().lower()
        if source not in {"camoufox", "chrome"}:
            raise ValueError("browser_engine 仅支持 camoufox 或 chrome")
        return source

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, value: str) -> str:
        source = value.strip().lower()
        if source not in {"protocol", "browser"}:
            raise ValueError("channel 仅支持 protocol 或 browser")
        return source


class MailSettings(BaseModel):
    provider: str = Field(default="yyds", max_length=20)
    api_base: str = Field(default="https://maliapi.215.im/v1", min_length=8, max_length=500)
    api_key: str = Field(default="", max_length=500)
    domains: list[str] = Field(default_factory=lambda: ["auto"], max_length=100)
    email_prefix: str = Field(default="", max_length=64)
    outlook_split_limit: int = Field(default=5, ge=1, le=50)
    email001_auto_purchase: bool = False
    email001_api_base: str = Field(default="https://email001.com", min_length=8, max_length=500)
    email001_api_key: str = Field(default="", max_length=500)
    email001_sku_id: int = Field(default=14, ge=1, le=100000)
    email001_quantity: int = Field(default=100, ge=1, le=1000)
    email001_purchase_timeout: float = Field(default=30, ge=10, le=300)

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        source = value.strip().lower()
        if source not in {"yyds", "outlook"}:
            raise ValueError("邮箱来源仅支持 yyds 或 outlook")
        return source

    @field_validator("api_base")
    @classmethod
    def validate_url(cls, value: str) -> str:
        clean = value.strip().rstrip("/")
        if not clean.startswith(("http://", "https://")):
            raise ValueError("邮箱 API 地址必须以 http:// 或 https:// 开头")
        return clean

    @field_validator("email001_api_base")
    @classmethod
    def validate_email001_url(cls, value: str) -> str:
        clean = value.strip().rstrip("/")
        if not clean.startswith(("http://", "https://")):
            raise ValueError("email001 API 地址必须以 http:// 或 https:// 开头")
        return clean


class CloudSettings(BaseModel):
    enabled: bool = False
    server: str = Field(default="", max_length=500)
    auth_key: str = Field(default="", max_length=1000)
    use_capacity: bool = True
    capacity_limit: int = Field(default=60, ge=10, le=200)
    upload_accounts: bool = True
    use_proxy: bool = True
    monitor_enabled: bool = False
    monitor_interval_seconds: int = Field(default=30, ge=5, le=3600)
    monitor_concurrency: int = Field(default=5, ge=1, le=50)
    shortage_confirmations: int = Field(default=2, ge=1, le=10)
    monitor_batch_limit: int = Field(default=20, ge=1, le=100)

    @field_validator("server")
    @classmethod
    def validate_server(cls, value: str) -> str:
        clean = value.strip().rstrip("/")
        if clean and not clean.startswith(("http://", "https://")):
            raise ValueError("云端地址必须以 http:// 或 https:// 开头")
        return clean

class NotificationSettings(BaseModel):
    bark_enabled: bool = False
    bark_url: str = Field(default="https://api.day.app", max_length=500)
    bark_key: str = Field(default="", max_length=500)
    bark_low_stock_threshold: int = Field(default=100, ge=1, le=100000)
    bark_check_interval_seconds: int = Field(default=30, ge=5, le=3600)
    bark_report_enabled: bool = False
    bark_report_interval_seconds: int = Field(default=3600, ge=60, le=86400)

    @field_validator("bark_url")
    @classmethod
    def validate_bark_url(cls, value: str) -> str:
        clean = value.strip().rstrip("/")
        if not clean.startswith(("http://", "https://")):
            raise ValueError("Bark 地址必须以 http:// 或 https:// 开头")
        return clean


class FlareSolverrSettings(BaseModel):
    enabled: bool = False
    url: str = Field(default="http://flaresolverr:8191", max_length=500)
    max_timeout_ms: int = Field(default=60000, ge=1000, le=300000)
    pass_proxy: bool = True


class SentinelSettings(BaseModel):
    so_enabled: bool = True
    so_required: bool = False
    node: str = Field(default="node", max_length=500)
    timeout_ms: int = Field(default=75000, ge=30000, le=300000)


class HealthSettings(BaseModel):
    auto_check_enabled: bool = False
    interval_seconds: int = Field(default=300, ge=60, le=86400)
    concurrency: int = Field(default=3, ge=1, le=50)
    recovery_concurrency: int = Field(default=3, ge=1, le=50)
    request_timeout: float = Field(default=30, ge=10, le=180)


class SettingsRequest(BaseModel):
    registration: RegistrationSettings
    mail: MailSettings
    cloud: CloudSettings
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    flaresolverr: FlareSolverrSettings
    sentinel: SentinelSettings
    health: HealthSettings


class StartRequest(BaseModel):
    count: int = Field(ge=1, le=100)
    concurrency: int = Field(ge=1, le=50)
    channel: str | None = None
    force: bool = False

    @field_validator("channel")
    @classmethod
    def validate_channel(cls, value: str | None) -> str | None:
        if value is None:
            return None
        source = value.strip().lower()
        if source not in {"protocol", "browser"}:
            raise ValueError("channel 仅支持 protocol 或 browser")
        return source


class StopRequest(BaseModel):
    pass


class RegistrationConcurrencyRequest(BaseModel):
    concurrency: int = Field(ge=1, le=50)


class OutlookPoolImportRequest(BaseModel):
    items: str = Field(min_length=1, max_length=1_000_000)


class DeleteOutlookPoolRequest(BaseModel):
    mailbox_ids: list[str] = Field(default_factory=list, max_length=1000)
    clear_all: bool = False


class DeleteAccountsRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list, max_length=200)


class HealthCheckRequest(BaseModel):
    account_ids: list[str] = Field(default_factory=list, max_length=200)


def _record_outlook_import_stats(store: JsonStore, result: dict[str, Any], actor: str) -> dict[str, Any]:
    day = china_today()
    added = int(result.get("added") or 0)
    updated = int(result.get("updated") or 0)
    source = "api" if actor == "api" else "ui"
    imported_at = iso_now()

    def update(raw: Any) -> dict[str, Any]:
        payload = raw if isinstance(raw, dict) else {}
        days = payload.setdefault("days", {})
        entry = days.setdefault(day, {
            "date": day,
            "added": 0,
            "updated": 0,
            "requests": 0,
            "api_added": 0,
            "api_updated": 0,
            "api_requests": 0,
            "ui_added": 0,
            "ui_updated": 0,
            "ui_requests": 0,
            "last_at": "",
        })
        entry["date"] = day
        entry["added"] = int(entry.get("added") or 0) + added
        entry["updated"] = int(entry.get("updated") or 0) + updated
        entry["requests"] = int(entry.get("requests") or 0) + 1
        entry[f"{source}_added"] = int(entry.get(f"{source}_added") or 0) + added
        entry[f"{source}_updated"] = int(entry.get(f"{source}_updated") or 0) + updated
        entry[f"{source}_requests"] = int(entry.get(f"{source}_requests") or 0) + 1
        entry["last_at"] = imported_at
        payload["days"] = days
        payload["last_import"] = {
            "at": imported_at,
            "source": source,
            "added": added,
            "updated": updated,
            "requests": 1,
        }
        return payload

    return store.update("outlook_import_stats.json", {"days": {}}, update)


def create_app(
    store: JsonStore | None = None,
    manager: RegistrationManager | None = None,
    monitor: CloudRegistrationMonitor | None = None,
    health_service: AccountHealthService | None = None,
) -> FastAPI:
    data_root = Path(os.getenv("REG_DATA_DIR") or (ROOT / "data"))
    runtime_store = store or JsonStore(data_root)
    users = UserStore(runtime_store)
    users.ensure_default()
    signer = SessionSigner(runtime_store)
    app_metadata = runtime_store.read("app.json", {})
    if not isinstance(app_metadata, dict):
        app_metadata = {}
    outlook_import_api_key = str(
        os.getenv("REG_OUTLOOK_IMPORT_API_KEY")
        or app_metadata.get("outlook_import_api_key")
        or secrets.token_urlsafe(32)
    ).strip()
    if app_metadata.get("outlook_import_api_key") != outlook_import_api_key:
        app_metadata["outlook_import_api_key"] = outlook_import_api_key
        runtime_store.write("app.json", app_metadata)
    runtime_manager = manager or RegistrationManager(runtime_store)
    runtime_monitor = monitor or CloudRegistrationMonitor(runtime_store, runtime_manager)
    runtime_health = health_service or AccountHealthService(runtime_store, runtime_manager)
    runtime_bark = BarkStockNotifier(runtime_store, runtime_manager)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        cleared_outlook_leases = await run_in_threadpool(
            OutlookMailboxPool(runtime_store.path("outlook_mailboxes.json")).clear_leases
        )
        if cleared_outlook_leases:
            runtime_manager.log("warning", f"已清理 Outlook 号池残留租约：{cleared_outlook_leases} 个")
        runtime_health.start()
        runtime_monitor.start()
        runtime_bark.start()
        try:
            yield
        finally:
            runtime_bark.shutdown()
            runtime_monitor.shutdown()
            runtime_health.shutdown()
            runtime_manager.shutdown()

    app = FastAPI(title="GPT 自动注册工具", version="0.1.0", lifespan=lifespan)
    app.state.store = runtime_store
    app.state.manager = runtime_manager
    app.state.monitor = runtime_monitor
    app.state.health_service = runtime_health
    app.state.bark_notifier = runtime_bark

    def current_user(session_token: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> str:
        session = signer.verify(session_token or "")
        if session is None:
            raise HTTPException(status_code=401, detail="请先登录")
        return session.username

    def outlook_import_actor(
        request: Request,
        session_token: str | None = Cookie(default=None, alias=COOKIE_NAME),
    ) -> str:
        session = signer.verify(session_token or "")
        if session is not None:
            return session.username
        supplied = str(request.headers.get("x-api-key") or "").strip()
        if supplied and hmac.compare_digest(supplied, outlook_import_api_key):
            return "api"
        raise HTTPException(status_code=401, detail="Outlook 导入 API Key 不正确")

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "service": "gpt-reg-tools"}

    @app.post("/api/auth/login")
    async def login(body: LoginRequest, response: Response) -> dict[str, Any]:
        authenticated = await run_in_threadpool(users.authenticate, body.username, body.password)
        if not authenticated:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = signer.issue(body.username.strip())
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=signer.ttl_seconds,
            httponly=True,
            samesite="lax",
            secure=False,
            path="/",
        )
        return {"ok": True, "username": body.username.strip()}

    @app.get("/api/auth/session")
    async def auth_session(session_token: str | None = Cookie(default=None, alias=COOKIE_NAME)) -> dict[str, Any]:
        session = signer.verify(session_token or "")
        if session is None:
            return {"authenticated": False, "username": ""}
        return {"authenticated": True, "username": session.username}

    @app.post("/api/auth/logout")
    async def logout(response: Response) -> dict[str, bool]:
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.post("/api/auth/password")
    async def change_password(
        body: PasswordRequest,
        response: Response,
        username: str = Depends(current_user),
    ) -> dict[str, bool]:
        try:
            await run_in_threadpool(users.change_password, username, body.current_password, body.new_password)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/dashboard")
    async def dashboard(_user: str = Depends(current_user)) -> dict[str, Any]:
        job_status = runtime_manager.status()
        return {
            "job": job_status,
            "jobs": job_status.get("providers", {}),
            "monitor": runtime_monitor.status(),
            "bark": runtime_bark.status(),
            "health": runtime_health.status(),
            "accounts": await run_in_threadpool(runtime_manager.account_summary),
            "registration_report": await run_in_threadpool(runtime_manager.registration_report),
        }

    @app.get("/api/settings")
    async def get_settings(_user: str = Depends(current_user)) -> dict[str, Any]:
        return await run_in_threadpool(runtime_store.settings)

    @app.put("/api/settings")
    async def save_settings(
        body: SettingsRequest,
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        payload = body.model_dump()
        await run_in_threadpool(runtime_store.write, "settings.json", payload)
        runtime_monitor.wake()
        runtime_health.wake()
        runtime_manager.log("success", "设置已保存到 data/settings.json")
        return payload

    @app.get("/api/settings/outlook-pool")
    async def outlook_pool_summary(_user: str = Depends(current_user)) -> dict[str, int]:
        settings = await run_in_threadpool(runtime_store.settings)
        mail = settings.get("mail") if isinstance(settings.get("mail"), dict) else {}
        return await run_in_threadpool(
            OutlookMailboxPool(runtime_store.path("outlook_mailboxes.json")).summary,
            int(mail.get("outlook_split_limit") or 5),
        )

    @app.get("/api/outlook-pool")
    async def outlook_pool_list(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=10, le=100),
        query: str = Query(default="", max_length=200),
        status: str = Query(default="all", max_length=20),
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        settings = await run_in_threadpool(runtime_store.settings)
        mail = settings.get("mail") if isinstance(settings.get("mail"), dict) else {}
        result = await run_in_threadpool(
            OutlookMailboxPool(runtime_store.path("outlook_mailboxes.json")).snapshot,
            int(mail.get("outlook_split_limit") or 5),
            page=page,
            page_size=page_size,
            query=query,
            status=status,
        )
        result["import_api"] = {
            "endpoint": "/api/outlook-pool/import",
            "header": "x-api-key",
            "api_key": outlook_import_api_key,
        }
        return result

    @app.get("/api/outlook-mails")
    async def outlook_mail_list(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=10, le=100),
        query: str = Query(default="", max_length=200),
        status: str = Query(default="all", max_length=20),
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        settings = await run_in_threadpool(runtime_store.settings)
        mail = settings.get("mail") if isinstance(settings.get("mail"), dict) else {}
        result = await run_in_threadpool(
            OutlookMailboxPool(runtime_store.path("outlook_mailboxes.json")).snapshot,
            int(mail.get("outlook_split_limit") or 5),
            page=page,
            page_size=page_size,
            query=query,
            status=status,
        )
        stats = await run_in_threadpool(runtime_store.read, "outlook_import_stats.json", {"days": {}})
        days = stats.get("days") if isinstance(stats, dict) and isinstance(stats.get("days"), dict) else {}
        ordered_days = [days[key] for key in sorted(days, reverse=True) if isinstance(days[key], dict)][:30]
        today = ordered_days[0] if ordered_days and str(ordered_days[0].get("date") or "") == china_today() else {}
        last_import = stats.get("last_import") if isinstance(stats, dict) and isinstance(stats.get("last_import"), dict) else {}
        if not last_import:
            latest_day = max(
                (entry for entry in ordered_days if str(entry.get("last_at") or "")),
                key=lambda entry: str(entry.get("last_at") or ""),
                default={},
            )
            if latest_day:
                last_import = {
                    "at": str(latest_day.get("last_at") or ""),
                    "source": "legacy",
                    "added": int(latest_day.get("added") or 0),
                    "updated": int(latest_day.get("updated") or 0),
                    "requests": int(latest_day.get("requests") or 0),
                }
        result["import_stats"] = {
            "today": today,
            "recent": ordered_days,
            "last_import": last_import,
        }
        return result

    @app.get("/api/outlook-mails/export.txt")
    async def export_outlook_mails(
        format: str = Query(default="detail", max_length=20),
        _user: str = Depends(current_user),
    ) -> PlainTextResponse:
        detail = str(format or "detail").strip().lower() != "raw"
        content = await run_in_threadpool(
            OutlookMailboxPool(runtime_store.path("outlook_mailboxes.json")).export_text,
            detail=detail,
        )
        filename = "outlook-mailboxes-detail.txt" if detail else "outlook-mailboxes.txt"
        return PlainTextResponse(
            content,
            media_type="text/plain",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/outlook-pool/import")
    async def import_outlook_pool_api(
        request: Request,
        _actor: str = Depends(outlook_import_actor),
    ) -> dict[str, Any]:
        content_type = str(request.headers.get("content-type") or "").lower()
        try:
            payload = await request.json() if "application/json" in content_type else (await request.body()).decode("utf-8")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="请求正文不是有效的 JSON 或 UTF-8 文本") from exc
        try:
            result = await run_in_threadpool(
                OutlookMailboxPool(runtime_store.path("outlook_mailboxes.json")).import_payload,
                payload,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        stats = await run_in_threadpool(_record_outlook_import_stats, runtime_store, result, _actor)
        runtime_manager.log("success", f"Outlook 导入 API：新增 {result['added']}，更新 {result['updated']}")
        return {"ok": True, **result, "import_stats": stats.get("days", {}).get(china_today(), {})}

    @app.post("/api/settings/outlook-pool/import")
    async def import_outlook_pool(
        body: OutlookPoolImportRequest,
        _user: str = Depends(current_user),
    ) -> dict[str, int]:
        try:
            result = await run_in_threadpool(
                OutlookMailboxPool(runtime_store.path("outlook_mailboxes.json")).import_text,
                body.items,
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        runtime_manager.log("success", f"Outlook 邮箱池已导入：新增 {result['added']}，更新 {result['updated']}")
        await run_in_threadpool(_record_outlook_import_stats, runtime_store, result, "ui")
        return result

    @app.delete("/api/outlook-pool")
    async def delete_outlook_pool(
        body: DeleteOutlookPoolRequest,
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        mailbox_ids = list(dict.fromkeys(str(value or "").strip() for value in body.mailbox_ids if str(value or "").strip()))
        if not body.clear_all and not mailbox_ids:
            raise HTTPException(status_code=400, detail="请选择要删除的 Outlook 邮箱")
        result = await run_in_threadpool(
            OutlookMailboxPool(runtime_store.path("outlook_mailboxes.json")).delete,
            mailbox_ids,
            clear_all=body.clear_all,
        )
        operation = "已清空" if body.clear_all else "已删除所选"
        runtime_manager.log("warning", f"Outlook 邮箱池{operation}：{result['removed']} 个")
        return {"ok": True, **result}

    @app.delete("/api/outlook-pool/failed")
    async def delete_failed_outlook_pool(
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        result = await run_in_threadpool(
            OutlookMailboxPool(runtime_store.path("outlook_mailboxes.json")).delete_failed,
        )
        runtime_manager.log("warning", f"Outlook 异常邮箱已全部删除：{result['removed']} 个")
        return {"ok": True, **result}

    @app.put("/api/settings/registration/concurrency")
    async def save_registration_concurrency(
        body: RegistrationConcurrencyRequest,
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        def update(raw: Any) -> dict[str, Any]:
            settings = deep_merge(DEFAULT_SETTINGS, raw if isinstance(raw, dict) else {})
            registration = settings.setdefault("registration", {})
            providers = registration.setdefault("providers", {})
            provider_settings = providers.setdefault("openai", {})
            provider_settings["concurrency"] = body.concurrency
            registration["concurrency"] = body.concurrency
            return settings

        await run_in_threadpool(runtime_store.update, "settings.json", DEFAULT_SETTINGS, update)
        runtime_monitor.wake()
        runtime_manager.log("success", f"ChatGPT 注册并发已保存：{body.concurrency}")
        return {"provider": "openai", "concurrency": body.concurrency}

    @app.post("/api/registration/start")
    async def start_registration(
        body: StartRequest,
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        settings = await run_in_threadpool(runtime_store.settings)
        registration_settings = settings.get("registration") if isinstance(settings.get("registration"), dict) else {}
        registration_channel = str(body.channel or registration_settings.get("channel") or "protocol").strip().lower()
        mail_settings = settings.get("mail") if isinstance(settings.get("mail"), dict) else {}
        mail_provider = str(mail_settings.get("provider") or "yyds").strip().lower()
        if mail_provider == "outlook":
            outlook_summary = await run_in_threadpool(
                OutlookMailboxPool(runtime_store.path("outlook_mailboxes.json")).summary,
                int(mail_settings.get("outlook_split_limit") or 5),
            )
            if int(outlook_summary.get("available_slots") or 0) < 1:
                raise HTTPException(status_code=400, detail="Outlook 邮箱池没有可用分裂邮箱")
        elif not str(mail_settings.get("api_key") or "").strip():
            raise HTTPException(status_code=400, detail="请先在设置中填写邮箱 API Key")
        current_status = runtime_manager.status()
        if current_status.get("state") in {"running", "stopping"}:
            raise HTTPException(status_code=409, detail="当前平台注册任务正在运行")
        def update_concurrency(raw: Any) -> dict[str, Any]:
            saved = deep_merge(DEFAULT_SETTINGS, raw if isinstance(raw, dict) else {})
            registration = saved.setdefault("registration", {})
            providers = registration.setdefault("providers", {})
            providers["openai"] = {
                **(providers.get("openai") if isinstance(providers.get("openai"), dict) else {}),
                "count": body.count,
                "concurrency": body.concurrency,
            }
            registration["count"] = body.count
            registration["concurrency"] = body.concurrency
            registration["channel"] = registration_channel
            registration["provider"] = "openai"
            return saved

        await run_in_threadpool(runtime_store.update, "settings.json", DEFAULT_SETTINGS, update_concurrency)
        registration_settings = settings.setdefault("registration", {})
        provider_settings = registration_settings.setdefault("providers", {}).setdefault("openai", {})
        provider_settings.update({"count": body.count, "concurrency": body.concurrency})
        registration_settings["count"] = body.count
        registration_settings["concurrency"] = body.concurrency
        registration_settings["channel"] = registration_channel
        registration_settings["provider"] = "openai"
        effective_count = body.count
        capacity_payload: dict[str, Any] | None = None
        cloud_settings = settings.get("cloud") if isinstance(settings.get("cloud"), dict) else {}
        if bool(cloud_settings.get("enabled")) and not body.force:
            server = str(cloud_settings.get("server") or "").strip()
            auth_key = str(cloud_settings.get("auth_key") or "").strip()
            if not server or not auth_key:
                raise HTTPException(status_code=400, detail="请先完善云端地址和管理员密钥")
            if bool(cloud_settings.get("use_capacity", True)):
                runtime_manager.log("info", f"注册前读取云端容量：{server.rstrip('/')}/api/image-pool/capacity")
                try:
                    capacity_payload = await run_in_threadpool(
                        CloudClient(
                            cloud_settings,
                            str(settings.get("registration", {}).get("proxy") or ""),
                        ).capacity
                    )
                except Exception as exc:
                    runtime_manager.log("warning", f"云端容量读取失败，按手动数量继续：{str(exc)[:500]}")
                else:
                    estimate = capacity_estimate(capacity_payload)
                    status = estimate["status"]
                    need = int(estimate["recommended_register_accounts"] or 0)
                    runtime_manager.log(
                        "info",
                        "云端容量评估："
                        f"状态：{capacity_status_label(status)}，建议注册：{need}，"
                        f"当前可调度账号：{estimate['current_effective_accounts']}，"
                        f"缺可用账号：{estimate['recommended_add_usable_accounts']}，"
                        f"可调度槽位：{estimate['dispatchable_slots']}，"
                        f"空闲槽位：{estimate['idle_slots']}，租用槽位：{estimate['leased_slots']}，"
                        f"冷却中：{estimate['cooling']}，受限账号：{estimate['limited']}，"
                        f"无效账号：{estimate['invalid']}，死号：{estimate['dead']}",
                    )
                    if status in {"idle", "enough", "saturated"} or need <= 0:
                        message = estimate["message"] or "云端容量充足，本轮跳过注册"
                        result = runtime_manager.mark_skipped(message, channel=registration_channel)
                        result["skipped"] = True
                        result["cloud_capacity"] = capacity_payload
                        return result
                    if status == "shortage":
                        effective_count = min(body.count, max(1, need))
                        runtime_manager.log("info", f"按云端缺口启动注册：目标 {effective_count} 个")
        try:
            if body.force:
                runtime_manager.log("warning", f"强制补号：按页面数量 {body.count}、并发 {body.concurrency} 启动")
            result = runtime_manager.start(
                count=effective_count,
                concurrency=body.concurrency,
                channel=registration_channel,
            )
            result["requested_count"] = body.count
            result["forced"] = body.force
            if capacity_payload is not None:
                result["cloud_capacity"] = capacity_payload
            return result
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/registration/stop")
    async def stop_registration(
        body: StopRequest | None = None,
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        return runtime_manager.stop()

    @app.get("/api/monitor/status")
    async def monitor_status(_user: str = Depends(current_user)) -> dict[str, Any]:
        return runtime_monitor.status()

    @app.post("/api/monitor/start")
    async def start_monitor(_user: str = Depends(current_user)) -> dict[str, Any]:
        runtime_manager.log("success", "自动监听已开启")
        return runtime_monitor.set_enabled(True)

    @app.post("/api/monitor/stop")
    async def stop_monitor(_user: str = Depends(current_user)) -> dict[str, Any]:
        runtime_manager.log("warning", "自动监听已停止")
        return runtime_monitor.set_enabled(False)

    @app.get("/api/accounts/health/status")
    async def account_health_status(_user: str = Depends(current_user)) -> dict[str, Any]:
        return runtime_health.status()

    @app.post("/api/accounts/health/check")
    async def check_account_health(
        body: HealthCheckRequest,
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            return runtime_health.start_check(body.account_ids, source="manual")
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/accounts/health/stop")
    async def stop_account_health(_user: str = Depends(current_user)) -> dict[str, Any]:
        return runtime_health.stop_check()

    @app.post("/api/accounts/health/auto/start")
    async def start_auto_health(_user: str = Depends(current_user)) -> dict[str, Any]:
        runtime_manager.log("success", "账号自动检测已开启")
        return runtime_health.set_auto_enabled(True)

    @app.post("/api/accounts/health/auto/stop")
    async def stop_auto_health(_user: str = Depends(current_user)) -> dict[str, Any]:
        runtime_manager.log("warning", "账号自动检测已停止")
        return runtime_health.set_auto_enabled(False)

    @app.get("/api/registration/status")
    async def registration_status(_user: str = Depends(current_user)) -> dict[str, Any]:
        return runtime_manager.status()

    @app.get("/api/logs")
    async def logs(cursor: int = 0, _user: str = Depends(current_user)) -> dict[str, Any]:
        return runtime_manager.logs(cursor)

    @app.get("/api/accounts")
    async def accounts(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=5, le=100),
        query: str = Query(default="", max_length=100),
        category: str = Query(default="all", pattern="^(all|alive|recovery|recovered|attention)$"),
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        return await run_in_threadpool(
            runtime_manager.accounts_page,
            page=page,
            page_size=page_size,
            query=query,
            category=category,
        )

    @app.get("/api/cloud/capacity")
    async def cloud_capacity(_user: str = Depends(current_user)) -> dict[str, Any]:
        settings = await run_in_threadpool(runtime_store.settings)
        cloud_settings = settings.get("cloud") if isinstance(settings.get("cloud"), dict) else {}
        try:
            return await run_in_threadpool(
                CloudClient(
                    cloud_settings,
                    str(settings.get("registration", {}).get("proxy") or ""),
                ).capacity
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/accounts/{account_id}/credentials")
    async def account_credentials(account_id: str, _user: str = Depends(current_user)) -> dict[str, Any]:
        try:
            return await run_in_threadpool(runtime_manager.account_credentials, account_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="账号不存在") from exc

    @app.delete("/api/accounts")
    async def delete_accounts(
        body: DeleteAccountsRequest,
        _user: str = Depends(current_user),
    ) -> dict[str, Any]:
        removed = await run_in_threadpool(runtime_manager.delete_accounts, body.account_ids)
        return {"ok": True, "removed": removed}

    @app.get("/api/accounts/export")
    async def export_accounts(_user: str = Depends(current_user)) -> Response:
        items = await run_in_threadpool(runtime_manager.accounts, include_secrets=True)
        payload = json.dumps(items, ensure_ascii=False, indent=2) + "\n"
        return Response(
            payload,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="accounts.json"'},
        )

    @app.get("/{requested_path:path}")
    async def static_files(requested_path: str, request: Request) -> Response:
        if requested_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="接口不存在")
        candidate = WEB_ROOT / requested_path if requested_path else WEB_ROOT / "index.html"
        try:
            candidate = candidate.resolve()
            candidate.relative_to(WEB_ROOT.resolve())
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="文件不存在") from exc
        response_path = candidate if candidate.is_file() else WEB_ROOT / "index.html"
        response = FileResponse(response_path)
        # Local admin UI changes often during rollback/redeploy; avoid Chrome keeping an old app.js
        # that leaves the boot screen stuck on “正在载入”.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    return app


app = create_app()
