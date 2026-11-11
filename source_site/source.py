"""合成来源行为，可直接调用或通过本地 HTTP 访问。

RPA 接入端只允许通过 HTTP 使用 /login、/form、/submit、/query 四个业务页面；
/__control 与 /__calls 是本地运维入口，业务流程不得调用，Source 的内部方法
也不是业务接口。
"""
import hashlib
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs

DATA = Path(__file__).resolve().parents[1] / "sources"

# 公开合成账号：账号 -> (口令, 所属组织)。口令仅用于本机模拟器。
ACCOUNTS = {
    "demo-org": ("local-only", "ORG-A"),
    "demo-org2": ("local-only-2", "ORG-B"),
}

# 历史上直接调用 Source.request 时使用的固定 Cookie，等价于 ORG-A 会话。
LEGACY_SESSION = "synthetic"


def json_file(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def _session_token(organization):
    return "synthetic-" + organization.lower()


class Source:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = []
        self.phase = "base"
        self.state = {}

    # ---- 内部状态辅助 -------------------------------------------------
    def _sessions(self):
        # token -> organization；被作废的会话映射为 None。
        return self.state.setdefault("sessions", {LEGACY_SESSION: "ORG-A"})

    def _csrf_tokens(self):
        return self.state.setdefault("csrf_tokens", {})

    def _receipts(self):
        return self.state.setdefault("receipts", [])

    def _organization_for_cookie(self, header_value):
        for part in (header_value or "").split(";"):
            part = part.strip()
            if part.startswith("session="):
                return self._sessions().get(part[len("session="):])
        return None

    def _invalidate_session(self, header_value):
        token = self._cookie_token(header_value)
        if token and token in self._sessions():
            self._sessions()[token] = None

    def _cookie_token(self, header_value):
        for part in (header_value or "").split(";"):
            part = part.strip()
            if part.startswith("session="):
                return part[len("session="):]
        return None

    # ---- 入口 ---------------------------------------------------------
    def request(self, method, path, query=None, headers=None, body=b""):
        query, headers = query or {}, headers or {}
        self.calls.append({"method": method, "path": path, "query": query})
        if path == "/__control" and method == "POST":
            control = json.loads(body or b"{}")
            new_phase = control.get("phase", self.phase)
            if new_phase == "expired" and self.phase != "expired":
                # 会话代次 +1：代次之前建立的会话全部视为旧会话。
                self.state["session_generation"] = (
                    int(self.state.get("session_generation", 0)) + 1)
            self.phase = new_phase
            self.state.update(control.get("state", {}))
            return 200, {}, {"phase": self.phase}
        if path == "/__calls":
            return 200, {}, self.calls[:-1]
        return self.dispatch(method, path, query, headers, body)

    def dispatch(self, method, path, q, h, body):
        if path == "/login" and method == "POST":
            # 任何阶段都允许凭正确账号重新登录，取得全新会话。
            return self._handle_login(body)
        organization = self._organization_for_cookie(h.get("cookie", ""))
        if organization is None:
            return 401, {}, {"error": "会话已过期", "written": False}
        if self.phase == "expired":
            # 模拟门户侧会话轮换：只作废旧会话；expired 开始后重新登录的
            # 新会话代次匹配，依旧可用，贴近真实门户的重新登录语义。
            token = self._cookie_token(h.get("cookie", ""))
            generation = int(self.state.get("session_generation", 0))
            logged_at = self.state.setdefault("session_logged_in_at", {})
            if int(logged_at.get(token, -1)) < generation:
                self._invalidate_session(h.get("cookie", ""))
                return 401, {}, {"error": "会话已过期", "written": False}
        if path == "/form":
            return self._handle_form(organization)
        if path == "/submit" and method == "POST":
            return self._handle_submit(organization, body)
        if path == "/query":
            return self._handle_query(organization, q)
        return 404, {}, {"error": "页面不存在"}

    # ---- 业务页面 -----------------------------------------------------
    def _handle_login(self, body):
        form = parse_qs(body.decode())
        account = (form.get("account") or [""])[0]
        password = (form.get("password") or [""])[0]
        expected = ACCOUNTS.get(account)
        if expected is None or expected[0] != password:
            return 401, {}, {"error": "登录失败", "written": False}
        organization = expected[1]
        token = _session_token(organization)
        self._sessions()[token] = organization
        # 记录登录时的会话代次：expired 轮换后旧代次会话会被拒，新登录可用。
        self.state.setdefault("session_logged_in_at", {})[token] = int(
            self.state.get("session_generation", 0))
        return (
            200,
            {"Set-Cookie": f"session={token}; HttpOnly; SameSite=Strict"},
            {"organization": organization},
        )

    def _handle_form(self, organization):
        token_no = int(self.state.get("token_no", 0)) + 1
        self.state["token_no"] = token_no
        csrf = str(token_no)
        self._csrf_tokens()[_session_token(organization)] = csrf
        html = (
            '<form action="/submit" method="post">'
            '<input name="csrf" value="%s">'
            '<input name="organization"><input name="activity_id">'
            '<input name="date"><input name="participants">'
            '<input name="summary_hash"></form>'
        ) % csrf
        return 200, {"Content-Type": "text/html; charset=utf-8"}, html.encode()

    def _handle_submit(self, organization, body):
        form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        session = _session_token(organization)
        expected_csrf = self._csrf_tokens().pop(session, None)
        if form.pop("csrf", "") != expected_csrf:
            return 403, {}, {"error": "表单令牌已失效", "written": False}
        try:
            participants = int(form.get("participants", "-1"))
        except ValueError:
            participants = -1
        if form.get("organization") != organization or participants < 0:
            return 422, {}, {"error": "组织或人数不合法", "written": False}
        receipts = self._receipts()
        receipt = {**form, "receipt": "R" + str(len(receipts) + 1)}
        receipts.append(receipt)
        if self.phase == "lost-response":
            # 记录已写入，但响应被截断并断开连接，模拟“提交后断线”。
            return 200, {"Content-Length": "100"}, b"{"
        return 200, {}, dict(receipt)

    def _handle_query(self, organization, q):
        if self.phase == "query-delay":
            # 查询延迟期间暂时查不到新回执，但不代表门户未写入。
            return 200, {}, {"receipts": []}
        wanted = {k: v[0] for k, v in q.items()}
        visible = [r for r in self._receipts() if r.get("organization") == organization]
        matched = [
            r for r in visible if all(r.get(k) == v for k, v in wanted.items())
        ]
        return 200, {}, {"receipts": matched}
