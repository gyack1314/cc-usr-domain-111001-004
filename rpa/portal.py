"""门户 HTTP 客户端。

只访问登录页 /login、表单页 /form、提交页 /submit、查询页 /query 四个页面，
不调用模拟器运维入口，也不导入模拟器内部模块。所有异常按“对写入结果的影响”
分类，供状态机区分处理。
"""
from __future__ import annotations

import http.client
import json
import socket
from html.parser import HTMLParser
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlencode

# 客户端允许访问的全部路径（白名单）。
ALLOWED_PATHS = ("/login", "/form", "/submit", "/query")


class PortalError(Exception):
    """门户交互异常基类。

    written 表示门户是否明确未写入：
    - False：响应明确告知未写入（业务拒绝、令牌失效、会话过期）；
    - None ：无法确定是否写入（超时、断连、响应截断），必须走查询核对。
    """

    written: bool | None = None

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        payload: Any = None,
        written: bool | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.payload = payload
        if written is not None:
            self.written = written


class LoginFailed(PortalError):
    """登录页明确拒绝账号/口令（401），未建立会话。"""

    written = False


class SessionExpired(PortalError):
    """业务页面返回 401：会话过期，需要重新登录。"""

    written = False


class CSRFTokenInvalid(PortalError):
    """提交页返回 403：单次 CSRF 令牌失效，且明确未写入。"""

    written = False


class BusinessRejected(PortalError):
    """提交页返回 422：业务校验失败，响应明确 written=False。"""

    written = False


class AmbiguousSubmission(PortalError):
    """超时、连接中断或响应截断：可能已写入，禁止直接重提。"""

    written = None


class _CSRFParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.csrf: str | None = None

    def handle_starttag(self, tag, attrs):
        if tag != "input":
            return
        data = dict(attrs)
        if data.get("name") == "csrf":
            self.csrf = data.get("value")


def parse_csrf(html_text: str) -> str:
    parser = _CSRFParser()
    parser.feed(html_text)
    if not parser.csrf:
        raise PortalError("表单页未包含 CSRF 令牌")
    return parser.csrf


class PortalClient:
    def __init__(
        self,
        base_url: str,
        account: str,
        password: str,
        *,
        timeout: float = 5.0,
        user_agent: str = "drill-form-rpa/1.0 (+local-synthetic)",
    ):
        self.base_url = base_url.rstrip("/")
        self.account = account
        self._password = password
        self.timeout = timeout
        self.user_agent = user_agent
        self._cookie: str | None = None
        self.organization: str | None = None

    # ---- 底层 HTTP ----------------------------------------------------
    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        if path not in ALLOWED_PATHS:
            raise PortalError(f"客户端只允许访问业务页面，拒绝路径: {path}")
        url = self.base_url + path
        if query:
            url += "?" + urlencode(query)
        return url

    def _request(self, path, *, method="GET", data=None, query=None, retry=0):
        body = None
        headers = {"User-Agent": self.user_agent}
        if data is not None:
            body = urlencode(data).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self._cookie:
            headers["Cookie"] = f"session={self._cookie}"
        req = urllib_request.Request(
            self._url(path, query), data=body, headers=headers, method=method
        )
        try:
            with urllib_request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return resp.status, dict(resp.headers), raw
        except urllib_error.HTTPError as exc:
            raw = exc.read() if exc.fp else b""
            payload = _safe_json(raw)
            if exc.code == 401:
                if path == "/login":
                    raise LoginFailed(
                        "登录页拒绝凭据", status=401, payload=payload
                    )
                raise SessionExpired(
                    "会话已过期", status=401, payload=payload, written=False
                )
            if exc.code == 403:
                raise CSRFTokenInvalid(
                    _error_text(payload, "表单令牌已失效"),
                    status=403,
                    payload=payload,
                    written=False,
                )
            if exc.code == 422:
                raise BusinessRejected(
                    _error_text(payload, "业务校验失败"),
                    status=422,
                    payload=payload,
                    written=False,
                )
            raise PortalError(
                f"门户返回未预期状态 {exc.code}",
                status=exc.code,
                payload=payload,
                written=None,
            )
        except (urllib_error.URLError, socket.timeout, TimeoutError,
                ConnectionError, http.client.IncompleteRead, OSError) as exc:
            # 连接在写入前后中断都无法区分，统一按“结果不明”处理。
            reason = type(exc).__name__
            if path == "/submit":
                raise AmbiguousSubmission(
                    f"提交请求连接中断（{reason}），写入结果不明", written=None
                ) from exc
            # 非提交页的网络错误不涉及写入，交由上层决定是否重试。
            raise PortalError(
                f"访问 {path} 时连接失败（{reason}）", written=None
            ) from exc

    # ---- 四个业务页面 -------------------------------------------------
    def login(self) -> str:
        status, headers, raw = self._request(
            "/login",
            method="POST",
            data={"account": self.account, "password": self._password},
        )
        set_cookie = headers.get("Set-Cookie") or ""
        token = None
        for part in set_cookie.split(";"):
            part = part.strip()
            if part.startswith("session="):
                token = part[len("session="):]
        if not token:
            raise PortalError("登录成功但未取得会话标识")
        self._cookie = token
        payload = _safe_json(raw) or {}
        self.organization = payload.get("organization")
        return token

    def fetch_form(self) -> str:
        status, headers, raw = self._request("/form")
        return parse_csrf(raw.decode("utf-8", errors="replace"))

    def submit(self, fields: dict[str, str], csrf_token: str) -> dict[str, Any]:
        data = {"csrf": csrf_token, **fields}
        try:
            status, headers, raw = self._request("/submit", method="POST", data=data)
        except AmbiguousSubmission:
            raise
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            # 响应截断/非完整 JSON：门户可能已经写入，按结果不明处理。
            raise AmbiguousSubmission(
                "提交响应不完整，写入结果不明", written=None
            ) from exc
        if not isinstance(payload, dict) or "receipt" not in payload:
            raise AmbiguousSubmission(
                "提交响应缺少回执，写入结果不明", written=None
            )
        return payload

    def query(self, filters: dict[str, str]) -> list[dict[str, Any]]:
        status, headers, raw = self._request("/query", query=filters)
        payload = _safe_json(raw)
        if not isinstance(payload, dict) or not isinstance(
            payload.get("receipts"), list
        ):
            raise PortalError("查询页响应结构无法识别")
        return payload["receipts"]

    # ---- 组合动作（带有限次会话自愈）---------------------------------
    def login_and_form(self) -> str:
        self.login()
        try:
            return self.fetch_form()
        except SessionExpired:
            # 旧会话在登录与取表单之间过期：重新登录后再取一次。
            self.login()
            return self.fetch_form()

    def submit_with_session(self, fields: dict[str, str]) -> dict[str, Any]:
        """登录→取表单→提交；会话过期最多自愈两轮，令牌失效不自动重放。"""
        last_exc = None
        for _ in range(2):
            try:
                token = self.login_and_form()
                return self.submit(fields, token)
            except SessionExpired as exc:
                # 401 明确未写入，重新登录后重试是安全的；CSRF/422 不在此列。
                last_exc = exc
                continue
        raise last_exc

    def query_with_session(self, filters: dict[str, str]) -> list[dict[str, Any]]:
        try:
            return self.query(filters)
        except SessionExpired:
            self.login()
            return self.query(filters)


def _safe_json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _error_text(payload: Any, default: str) -> str:
    if isinstance(payload, dict) and payload.get("error"):
        return str(payload["error"])
    return default
