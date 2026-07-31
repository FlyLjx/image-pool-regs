from __future__ import annotations

import base64
import json
import random
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Locator, Page, Playwright, sync_playwright

from app.registration.mail import YydsMailClient
from app.registration.outlook import OutlookMailClient
from app.registration.protocol import outlook_error_should_disable, random_password


LogCallback = Callable[[str, str], None]
CHATGPT_URL = "https://chatgpt.com"
DEFAULT_OUTLOOK_POOL_PATH = Path(__file__).resolve().parents[2] / "data" / "outlook_mailboxes.json"

EMAIL_SELECTORS = (
    'input#login-email',
    'input[type="email"]',
    'input[name="email"]',
    'input[name="username"]',
    'input[autocomplete="username"]',
    'input[inputmode="email"]',
)
PASSWORD_SELECTORS = (
    'input[type="password"]',
    'input[name="password"]',
    'input[autocomplete="new-password"]',
)
OTP_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[type="tel"]',
    'input[name*="code" i]',
    'input[id*="code" i]',
)
CONTINUE_SELECTORS = (
    'button[type="submit"]',
    'button[data-testid="continue-button"]',
    'button:has-text("Continue")',
    'button:has-text("Next")',
    'button:has-text("Create account")',
    'button:has-text("Sign up")',
    'button:has-text("继续")',
    'button:has-text("下一步")',
    'button:has-text("创建账号")',
    'button:has-text("注册")',
)
SIGNUP_SELECTORS = (
    'a:has-text("Sign up")',
    'button:has-text("Sign up")',
    'a:has-text("Create account")',
    'button:has-text("Create account")',
    'a:has-text("注册")',
    'button:has-text("注册")',
    'a:has-text("创建账号")',
    'button:has-text("创建账号")',
)
PASSWORDLESS_SELECTORS = (
    'button[name="intent"][value="passwordless_login_send_otp"]',
    'button[value="passwordless_login_send_otp"]',
    'button:has-text("one-time code")',
    'button:has-text("one time code")',
    'button:has-text("验证码")',
    'button:has-text("一次性验证码")',
)


def _jwt_claims(token: str) -> dict[str, Any]:
    try:
        payload = str(token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _account_id(token: str) -> str:
    claims = _jwt_claims(token)
    auth = claims.get("https://api.openai.com/auth")
    if isinstance(auth, dict) and str(auth.get("chatgpt_account_id") or "").strip():
        return str(auth["chatgpt_account_id"]).strip()
    return str(claims.get("sub") or "").strip()


def _proxy_config(value: str) -> dict[str, str] | None:
    clean = str(value or "").strip()
    if not clean:
        return None
    parsed = urlparse(clean if "://" in clean else f"http://{clean}")
    if not parsed.hostname or not parsed.port:
        raise ValueError("浏览器代理格式应为 http://host:port")
    result = {"server": f"{parsed.scheme or 'http'}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        result["username"] = parsed.username
    if parsed.password:
        result["password"] = parsed.password
    return result


def _visible(locator: Locator, timeout: int = 300) -> bool:
    try:
        locator.wait_for(state="visible", timeout=timeout)
        return True
    except Exception:
        return False


def _first_visible(page: Page, selectors: tuple[str, ...], timeout: int = 300) -> Locator | None:
    for selector in selectors:
        locator = page.locator(selector).first
        if _visible(locator, timeout):
            return locator
    return None


class BrowserRegistrar:
    """Runs the ChatGPT signup UI in a fresh Playwright Chromium profile."""

    def __init__(
        self,
        settings: dict[str, Any],
        logger: LogCallback | None = None,
        stop_event: Event | None = None,
    ) -> None:
        self.settings = settings
        self.registration = settings.get("registration") if isinstance(settings.get("registration"), dict) else {}
        self.mail_settings = settings.get("mail") if isinstance(settings.get("mail"), dict) else {}
        self.outlook_pool_path = Path(str(settings.get("outlook_pool_path") or DEFAULT_OUTLOOK_POOL_PATH))
        self.data_root = Path(str(settings.get("_data_root") or "data"))
        self.proxy = str(self.registration.get("proxy") or "").strip()
        self.timeout_seconds = max(20.0, float(self.registration.get("request_timeout") or 45))
        self.mail_timeout = max(30.0, float(self.registration.get("mail_wait_timeout") or 120))
        self.mail_interval = max(0.5, float(self.registration.get("mail_poll_interval") or 3))
        self.browser_engine = str(self.registration.get("browser_engine") or "camoufox").strip().lower()
        if self.browser_engine not in {"camoufox", "chrome"}:
            raise ValueError("浏览器后端仅支持 camoufox 或 chrome")
        self.headless = bool(self.registration.get("browser_headless", False))
        self.slow_mo = max(0, min(1000, int(self.registration.get("browser_slow_mo_ms") or 40)))
        self.logger = logger or (lambda _level, _message: None)
        self.stop_event = stop_event or Event()
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._browser_manager: Any = None
        self._profile_dir: Path | None = None
        self._closed = False
        self._last_api_error = ""

    def _log(self, message: str, level: str = "info") -> None:
        self.logger(level, message)

    def _check_stopped(self) -> None:
        if self.stop_event.is_set():
            raise RuntimeError("任务已停止")

    def _mail_client(self) -> Any:
        source = str(self.mail_settings.get("provider") or "yyds").strip().lower()
        if source == "outlook":
            return OutlookMailClient(
                self.outlook_pool_path,
                proxy=self.proxy,
                request_timeout=self.timeout_seconds,
                split_limit=int(self.mail_settings.get("outlook_split_limit") or 5),
                queue_wait_timeout=max(1800.0, self.mail_timeout * 8, self.timeout_seconds * 20),
                stopped=self.stop_event.is_set,
                on_status=self._log,
            )
        options = dict(self.mail_settings)
        options["request_timeout"] = self.timeout_seconds
        return YydsMailClient(options, self.proxy)

    def _launch(self) -> Page:
        if self.browser_engine == "camoufox":
            return self._launch_camoufox()
        return self._launch_chrome()

    def _configure_page(self, page: Page, label: str) -> Page:
        timeout_ms = int(self.timeout_seconds * 1000)
        page.set_default_timeout(timeout_ms)
        page.set_default_navigation_timeout(timeout_ms)
        page.on("response", self._capture_api_error)
        self._log(f"{label} 已启动：{'无头' if self.headless else '可见窗口'}，使用全新环境", "success")
        return page

    def _launch_camoufox(self) -> Page:
        try:
            from camoufox.sync_api import Camoufox
        except ImportError as exc:
            raise RuntimeError("Camoufox 未安装，请执行 pip install -e . 和 python -m camoufox fetch") from exc
        options: dict[str, Any] = {"headless": self.headless}
        if not self.headless:
            options["window"] = (1280, 800)
        proxy = _proxy_config(self.proxy)
        if proxy:
            options["proxy"] = proxy
            options["geoip"] = True
        try:
            self._browser_manager = Camoufox(**options)
            browser = self._browser_manager.__enter__()
            page = browser.new_page()
        except Exception as exc:
            raise RuntimeError(
                "Camoufox 启动失败，请执行 python -m camoufox fetch："
                f"{str(exc)[:300]}"
            ) from exc
        self._context = page.context
        return self._configure_page(page, "Camoufox / Playwright")

    def _launch_chrome(self) -> Page:
        profiles_root = self.data_root / "browser_profiles"
        profiles_root.mkdir(parents=True, exist_ok=True)
        self._profile_dir = Path(tempfile.mkdtemp(prefix="openai-", dir=profiles_root))
        self._playwright = sync_playwright().start()
        options: dict[str, Any] = {
            "headless": self.headless,
            "slow_mo": self.slow_mo,
            "channel": "chrome",
            "viewport": {"width": 1280, "height": 800},
            "locale": "en-US",
            "timezone_id": "America/Los_Angeles",
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-features=Translate,OptimizationHints",
                "--no-first-run",
            ],
            "ignore_default_args": ["--enable-automation"],
        }
        proxy = _proxy_config(self.proxy)
        if proxy:
            options["proxy"] = proxy
        try:
            self._context = self._playwright.chromium.launch_persistent_context(str(self._profile_dir), **options)
        except Exception as exc:
            raise RuntimeError(
                "Playwright Chromium 启动失败，请执行 python -m playwright install chromium："
                f"{str(exc)[:300]}"
            ) from exc
        self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        pages = self._context.pages
        page = pages[0] if pages else self._context.new_page()
        return self._configure_page(page, "Google Chrome / Playwright")

    def _capture_api_error(self, response: Any) -> None:
        try:
            if int(response.status) < 400 or "/api/accounts/" not in str(response.url):
                return
            text = response.body().decode("utf-8", errors="replace")[:500]
            self._last_api_error = f"HTTP {response.status}: {text}"
        except Exception:
            return

    def _goto(self, page: Page, url: str) -> None:
        last_error: Exception | None = None
        for attempt in range(1, 4):
            self._check_stopped()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=int(self.timeout_seconds * 1000))
                return
            except Exception as exc:
                last_error = exc
                self._log(f"页面打开异常，重试 {attempt}/3：{str(exc)[:180]}", "warning")
                time.sleep(attempt)
        raise RuntimeError(f"页面打开失败：{str(last_error)[:300]}")

    def _start_signup(self, page: Page, email: str) -> None:
        self._log("打开 ChatGPT 注册入口")
        self._goto(page, f"{CHATGPT_URL}/")
        page.wait_for_timeout(1200)
        body = self._page_text(page).lower()
        if "enable javascript and cookies" in body or "just a moment" in body:
            self._log("浏览器正在等待 Cloudflare 页面完成", "warning")
            page.wait_for_timeout(8000)

        device_id = str(uuid.uuid4())
        try:
            page.context.add_cookies(
                [
                    {"name": "oai-did", "value": device_id, "domain": ".chatgpt.com", "path": "/"},
                    {"name": "oai-did", "value": device_id, "domain": ".auth.openai.com", "path": "/"},
                ]
            )
            auth_url = page.evaluate(
                """
                async ({email, deviceId}) => {
                  const csrfResponse = await fetch('/api/auth/csrf', {credentials: 'include'});
                  if (!csrfResponse.ok) return '';
                  const csrf = await csrfResponse.json();
                  const query = new URLSearchParams({
                    prompt: 'login',
                    'ext-oai-did': deviceId,
                    auth_session_logging_id: crypto.randomUUID(),
                    screen_hint: 'login_or_signup',
                    login_hint: email,
                  });
                  const body = new URLSearchParams({
                    callbackUrl: 'https://chatgpt.com/',
                    csrfToken: csrf.csrfToken,
                    json: 'true',
                  });
                  const response = await fetch(`/api/auth/signin/openai?${query}`, {
                    method: 'POST', credentials: 'include', redirect: 'follow',
                    headers: {'content-type': 'application/x-www-form-urlencoded'}, body,
                  });
                  const data = await response.json();
                  return String(data.url || '');
                }
                """,
                {"email": email, "deviceId": device_id},
            )
            if auth_url:
                self._goto(page, str(auth_url))
                self._wait_cloudflare(page)
                return
        except Exception as exc:
            self._log(f"NextAuth 入口未就绪，切换页面入口：{str(exc)[:160]}", "warning")

        self._goto(page, f"{CHATGPT_URL}/auth/login")
        signup = _first_visible(page, SIGNUP_SELECTORS, 1200)
        if signup:
            signup.click()

    def _cloudflare_visible(self, page: Page) -> bool:
        try:
            title = str(page.title() or "").lower()
        except Exception:
            title = ""
        text = self._page_text(page).lower()
        return any(
            marker in f"{title}\n{text}"
            for marker in (
                "just a moment",
                "performing security verification",
                "verify you are human",
                "enable javascript and cookies",
            )
        )

    def _wait_cloudflare(self, page: Page) -> None:
        if not self._cloudflare_visible(page):
            return
        wait_seconds = max(20, min(300, int(self.registration.get("browser_cf_wait_seconds") or 120)))
        self._log(f"检测到 Cloudflare 验证，等待浏览器环境自动通过（最多 {wait_seconds} 秒）", "warning")
        deadline = time.monotonic() + wait_seconds
        next_log = time.monotonic() + 10
        while time.monotonic() < deadline:
            self._check_stopped()
            page.wait_for_timeout(1000)
            if not self._cloudflare_visible(page):
                self._log("Cloudflare 验证已通过", "success")
                return
            if time.monotonic() >= next_log:
                remaining = max(0, int(deadline - time.monotonic()))
                self._log(f"仍在等待 Cloudflare 验证，剩余 {remaining} 秒")
                next_log = time.monotonic() + 10
        raise RuntimeError("Cloudflare 验证等待超时；请检查当前代理出口或切换浏览器后端")

    @staticmethod
    def _page_text(page: Page) -> str:
        try:
            return str(page.locator("body").inner_text(timeout=1500) or "")
        except Exception:
            return ""

    def _click_continue(self, page: Page) -> None:
        button = _first_visible(page, CONTINUE_SELECTORS, 600)
        if button:
            try:
                button.wait_for(state="visible", timeout=3000)
                button.click(timeout=8000)
                return
            except Exception:
                pass
        focused = page.locator("input:focus").first
        if focused.count():
            focused.press("Enter")
            return
        raise RuntimeError("当前页面没有可用的继续按钮")

    def _fill_human(self, locator: Locator, value: str) -> None:
        locator.click()
        locator.fill("")
        locator.type(value, delay=random.randint(45, 90))
        try:
            locator.dispatch_event("blur")
        except Exception:
            pass

    def _wait_for_code(self, mail: Any, mailbox: dict[str, Any], requested_at: float) -> str:
        self._log("等待邮箱验证码")
        code = mail.wait_for_code(
            mailbox,
            requested_at=requested_at,
            timeout=self.mail_timeout,
            interval=self.mail_interval,
            stopped=self.stop_event.is_set,
            on_status=lambda message: self._log(message),
        )
        if not code:
            raise RuntimeError("等待邮箱验证码超时")
        self._log("已读取邮箱验证码", "success")
        return str(code)

    def _fill_otp(self, page: Page, code: str) -> None:
        inputs = page.locator(
            'input:visible[autocomplete="one-time-code"], input:visible[inputmode="numeric"], '
            'input:visible[type="tel"], input:visible[name*="code" i], input:visible[id*="code" i]'
        )
        count = inputs.count()
        if count >= len(code) and count > 1:
            for index, digit in enumerate(code):
                inputs.nth(index).fill(digit)
            return
        target = _first_visible(page, OTP_SELECTORS, 700)
        if not target:
            raise RuntimeError("验证码已收到，但页面没有验证码输入框")
        self._fill_human(target, code)

    def _fill_about_you(self, page: Page, full_name: str, birthdate: str) -> None:
        self._log("填写账号资料")
        name = _first_visible(
            page,
            (
                'input[name="name"]',
                'input[name="full_name"]',
                'input[autocomplete="name"]',
                'input[placeholder*="name" i]',
            ),
            500,
        )
        if name:
            self._fill_human(name, full_name)

        year, month, day = birthdate.split("-")
        birthday = _first_visible(
            page,
            (
                'input[name="birthday"]',
                'input[name="birthdate"]',
                'input[type="date"]',
                'input[placeholder*="birth" i]',
            ),
            400,
        )
        if birthday:
            applied = False
            for value in (f"{month}/{day}/{year}", birthdate):
                try:
                    self._fill_human(birthday, value)
                    applied = bool(birthday.input_value())
                except Exception:
                    applied = False
                if applied:
                    break
        else:
            age = _first_visible(
                page,
                ('input[name="age"]', 'input[placeholder*="age" i]', 'input[aria-label*="age" i]'),
                400,
            )
            if age:
                self._fill_human(age, str(datetime.now().year - int(year)))
            else:
                selects = page.locator("select:visible")
                if selects.count() >= 3:
                    for index, value in enumerate((str(int(month)), str(int(day)), year)):
                        try:
                            selects.nth(index).select_option(value=value)
                        except Exception:
                            selects.nth(index).select_option(label=value)
                else:
                    page.evaluate(
                        """
                        (value) => {
                          const input = document.querySelector('input[name="birthday"]');
                          if (!input) return false;
                          const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                          setter.call(input, value);
                          input.dispatchEvent(new Event('input', {bubbles: true}));
                          input.dispatchEvent(new Event('change', {bubbles: true}));
                          return true;
                        }
                        """,
                        birthdate,
                    )
        page.wait_for_timeout(500)
        self._click_continue(page)

    def _error_text(self, page: Page) -> str:
        text = self._page_text(page)
        patterns = (
            r"(sorry, we cannot create your account[^\n]*)",
            r"(this email[^\n]{0,160}(?:not supported|not allowed)[^\n]*)",
            r"(failed to create account[^\n]*)",
            r"(invalid authorization step[^\n]*)",
            r"(something went wrong[^\n]*)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return ""

    def _session(self, page: Page, *, wait_seconds: float = 45) -> dict[str, Any]:
        deadline = time.monotonic() + wait_seconds
        last_error = ""
        while time.monotonic() < deadline:
            self._check_stopped()
            if "chatgpt.com" not in str(page.url).lower():
                page.wait_for_timeout(800)
                continue
            try:
                payload = page.evaluate(
                    """
                    async () => {
                      const response = await fetch('/api/auth/session', {
                        credentials: 'include', headers: {accept: 'application/json'}
                      });
                      return {status: response.status, text: await response.text()};
                    }
                    """
                )
                data = json.loads(str(payload.get("text") or "{}"))
                token = str(data.get("accessToken") or data.get("access_token") or "").strip()
                if int(payload.get("status") or 0) == 200 and token:
                    cookies = page.context.cookies()
                    cookie_map = {str(item.get("name")): str(item.get("value")) for item in cookies}
                    user_agent = str(page.evaluate("navigator.userAgent") or "")
                    return {
                        "access_token": token,
                        "refresh_token": str(data.get("refreshToken") or data.get("refresh_token") or "").strip(),
                        "id_token": str(data.get("idToken") or data.get("id_token") or "").strip(),
                        "session_token": str(cookie_map.get("__Secure-next-auth.session-token") or ""),
                        "cookies": "; ".join(f"{key}={value}" for key, value in cookie_map.items() if value),
                        "user_agent": user_agent,
                        "profile": data.get("user") if isinstance(data.get("user"), dict) else {},
                        "expires_at": str(data.get("expires") or ""),
                    }
                last_error = f"HTTP {payload.get('status')}: {str(payload.get('text') or '')[:200]}"
            except Exception as exc:
                last_error = str(exc)
            page.wait_for_timeout(1000)
        raise RuntimeError(f"浏览器注册完成但 ChatGPT Session 未就绪：{last_error[:300]}")

    def _run_flow(
        self,
        page: Page,
        email: str,
        password: str,
        mail: Any,
        mailbox: dict[str, Any],
        full_name: str,
        birthdate: str,
    ) -> tuple[dict[str, Any], str]:
        self._start_signup(page, email)
        last_signature = ""
        repeated = 0
        requested_at = time.time()
        existing_account = False
        password_submitted = False
        otp_submitted = False

        for step in range(30):
            self._check_stopped()
            page.wait_for_timeout(500)
            url = str(page.url or "")
            path = urlparse(url).path.lower()
            if self._cloudflare_visible(page):
                self._wait_cloudflare(page)
                continue
            session = None
            if "chatgpt.com" in url.lower() and "/auth/" not in url.lower():
                try:
                    session = self._session(page, wait_seconds=3)
                except Exception:
                    session = None
            if session:
                self._log("浏览器账号 Session 已获取", "success")
                return session, "existing_otp" if existing_account else "signup"

            email_input = _first_visible(page, EMAIL_SELECTORS, 250)
            password_input = _first_visible(page, PASSWORD_SELECTORS, 250)
            otp_input = _first_visible(page, OTP_SELECTORS, 250)
            text = self._page_text(page)
            lowered = text.lower()
            is_about = "about you" in lowered or "tell us about you" in lowered or "关于你" in text
            signature = f"{url}|{bool(email_input)}|{bool(password_input)}|{bool(otp_input)}|{is_about}"
            if signature == last_signature:
                repeated += 1
            else:
                last_signature, repeated = signature, 0
            if repeated > 15:
                detail = self._error_text(page) or self._last_api_error or "页面状态长时间没有变化"
                raise RuntimeError(f"浏览器注册卡在当前步骤：{detail[:300]}")

            error = self._error_text(page)
            if error:
                raise RuntimeError(error)

            if email_input:
                current = str(email_input.input_value() or "").strip()
                if current.lower() != email.lower():
                    self._log(f"填写邮箱：{email}")
                    self._fill_human(email_input, email)
                requested_at = time.time()
                self._click_continue(page)
                continue

            if password_input:
                login_password = "log-in/password" in path or "welcome back" in lowered
                if login_password and not password_submitted:
                    if str(getattr(mail, "provider_name", "")) == "outlook":
                        passwordless = _first_visible(page, PASSWORDLESS_SELECTORS, 500)
                        if passwordless:
                            existing_account = True
                            requested_at = time.time()
                            self._log("Outlook 地址已注册，切换邮箱验证码登录", "warning")
                            passwordless.click()
                            continue
                    signup = _first_visible(page, SIGNUP_SELECTORS, 400)
                    if signup:
                        self._log("页面识别为已有登录路径，切换到创建账号", "warning")
                        signup.click()
                        continue
                    passwordless = _first_visible(page, PASSWORDLESS_SELECTORS, 500)
                    if passwordless:
                        existing_account = True
                        requested_at = time.time()
                        self._log("Outlook 地址已注册，切换邮箱验证码登录", "warning")
                        passwordless.click()
                        continue
                if password_submitted:
                    page.wait_for_timeout(800)
                    continue
                self._log("填写注册密码")
                self._fill_human(password_input, password)
                page.wait_for_timeout(1400)
                requested_at = time.time()
                self._click_continue(page)
                password_submitted = True
                continue

            if otp_input and not otp_submitted:
                code = self._wait_for_code(mail, mailbox, requested_at)
                self._fill_otp(page, code)
                self._log("提交邮箱验证码")
                page.wait_for_timeout(700)
                if _first_visible(page, OTP_SELECTORS, 300):
                    self._click_continue(page)
                else:
                    self._log("验证码页面已自动提交")
                otp_submitted = True
                continue

            if is_about or "about-you" in path:
                self._fill_about_you(page, full_name, birthdate)
                continue

            if "consent" in path or "workspace" in path or "organization" in path:
                self._log("确认授权页面")
                self._click_continue(page)
                continue

            signup = _first_visible(page, SIGNUP_SELECTORS, 250)
            if signup:
                self._log("点击创建账号入口")
                signup.click()
                continue
            if step and step % 8 == 0:
                self._log(f"等待页面加载：{url[:120]}")

        raise RuntimeError("浏览器注册状态机超过最大步骤")

    def _save_debug(self, page: Page, label: str = "failure") -> None:
        debug_root = self.data_root / "browser_debug"
        debug_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        base = debug_root / f"{stamp}-{label}"
        try:
            page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            base.with_suffix(".html").write_text(page.content(), encoding="utf-8")
            base.with_suffix(".json").write_text(
                json.dumps({"url": page.url, "api_error": self._last_api_error}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._log(f"浏览器现场已保存：{base.name}.*", "warning")
        except Exception:
            pass

    def register(self) -> dict[str, Any]:
        self._check_stopped()
        mail = self._mail_client()
        page: Page | None = None
        mailbox: dict[str, Any] | None = None
        committed = False
        try:
            source = str(getattr(mail, "provider_name", "yyds"))
            self._log("领取 Outlook 邮箱" if source == "outlook" else "创建临时邮箱")
            mailbox = mail.create_mailbox()
            email = str(mailbox["address"])
            password = random_password()
            first_name = random.choice(("James", "Robert", "John", "Michael", "David", "Mary", "Emma", "Olivia"))
            last_name = random.choice(("Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller"))
            full_name = f"{first_name} {last_name}"
            birthdate = f"{random.randint(1988, 2001):04d}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
            if source == "outlook":
                split_index = int(mailbox.get("split_index") or 0)
                if split_index == 0:
                    self._log(f"Outlook 母号就绪：{email}", "success")
                else:
                    self._log(f"Outlook 分裂号 #{split_index} 就绪：{email}", "success")
            self._log(f"邮箱就绪：{email}", "success")

            page = self._launch()
            session, registration_mode = self._run_flow(
                page, email, password, mail, mailbox, full_name, birthdate
            )
            commit_mailbox = getattr(mail, "commit_mailbox", None)
            if callable(commit_mailbox):
                commit_mailbox(mailbox)
                committed = True
                self._log("Outlook 邮箱已登记为已使用", "success")

            access_token = str(session.get("access_token") or "").strip()
            claims = _jwt_claims(access_token)
            expires_in = max(0, int(claims.get("exp") or 0) - int(time.time()))
            now = datetime.now(timezone.utc).isoformat()
            result = {
                "id": _account_id(access_token) or str(claims.get("sub") or uuid.uuid4().hex),
                "email": email,
                "password": "" if registration_mode == "existing_otp" else password,
                "access_token": access_token,
                "refresh_token": str(session.get("refresh_token") or ""),
                "id_token": str(session.get("id_token") or ""),
                "session_token": str(session.get("session_token") or ""),
                "cookies": str(session.get("cookies") or ""),
                "user_agent": str(session.get("user_agent") or ""),
                "token_type": "Bearer",
                "expires_in": expires_in,
                "source_type": "browser",
                "registration_channel": "browser",
                "mail_provider": source,
                "registration_mode": registration_mode,
                "created_at": now,
            }
            self._log(f"浏览器注册成功：{email}", "success")
            return result
        except Exception as exc:
            if page is not None:
                self._save_debug(page)
            fail_mailbox = getattr(mail, "fail_mailbox", None)
            if mailbox is not None and callable(fail_mailbox) and outlook_error_should_disable(exc):
                fail_mailbox(mailbox, str(exc))
                self._log(
                    f"Outlook 母号已标记失效，后续不再注册：{mailbox.get('base_address') or mailbox.get('address')}，原因：{str(exc)[:180]}",
                    "warning",
                )
            elif mailbox is not None and getattr(mail, "provider_name", "") == "outlook":
                self._log(
                    f"Outlook 母号本次失败为环境/中断类，释放回号池：{mailbox.get('base_address') or mailbox.get('address')}，原因：{str(exc)[:180]}",
                    "warning",
                )
            raise
        finally:
            if mailbox is not None and committed:
                mailbox = None
            mail.close()
            self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._browser_manager is not None:
            try:
                self._browser_manager.__exit__(None, None, None)
            except Exception:
                pass
            self._browser_manager = None
            self._context = None
        elif self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        if self._profile_dir is not None:
            try:
                shutil.rmtree(self._profile_dir, ignore_errors=True)
            except Exception:
                pass
            self._profile_dir = None
