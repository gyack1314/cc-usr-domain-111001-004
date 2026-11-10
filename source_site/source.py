"""合成来源行为，可直接调用或通过本地 HTTP 访问。"""
import hashlib
import json
import threading
from pathlib import Path
from urllib.parse import parse_qs

DATA = Path(__file__).resolve().parents[1] / "sources"


def json_file(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


class Source:
    def __init__(self):
        self.lock = threading.RLock()
        self.calls = []
        self.phase = "base"
        self.state = {}

    def request(self, method, path, query=None, headers=None, body=b""):
        query, headers = query or {}, headers or {}
        self.calls.append({"method": method, "path": path, "query": query})
        if path == "/__control" and method == "POST":
            control = json.loads(body or b"{}")
            self.phase = control.get("phase", self.phase)
            self.state.update(control.get("state", {}))
            return 200, {}, {"phase": self.phase}
        if path == "/__calls":
            return 200, {}, self.calls[:-1]
        return self.dispatch(method, path, query, headers, body)

    def dispatch(self, method, path, q, h, body):
        if path == "/login" and method == "POST":
            form = parse_qs(body.decode())
            if form.get("account") != ["demo-org"] or form.get("password") != ["local-only"]:
                return 401, {}, {"error": "登录失败"}
            self.state["logged_in"] = True
            return 200, {"Set-Cookie": "session=synthetic; HttpOnly; SameSite=Strict"}, {"organization": "ORG-A"}
        if "session=synthetic" not in h.get("cookie", "") or self.phase == "expired":
            return 401, {}, {"error": "会话已过期"}
        if path == "/form":
            token = str(self.state.get("token_no", 0) + 1)
            self.state["token_no"] = int(token)
            self.state["csrf"] = token
            html = f'<form action="/submit" method="post"><input name="csrf" value="{token}"><input name="organization"><input name="activity_id"><input name="date"><input name="participants"><input name="summary_hash"></form>'
            return 200, {"Content-Type": "text/html; charset=utf-8"}, html.encode()
        if path == "/submit" and method == "POST":
            form = {k: v[0] for k, v in parse_qs(body.decode()).items()}
            if form.pop("csrf", "") != self.state.pop("csrf", None):
                return 403, {}, {"error": "表单令牌已失效", "written": False}
            if form.get("organization") != "ORG-A" or int(form.get("participants", "-1")) < 0:
                return 422, {}, {"error": "组织或人数不合法", "written": False}
            receipts = self.state.setdefault("receipts", [])
            receipts.append({**form, "receipt": "R" + str(len(receipts)+1)})
            if self.phase == "lost-response":
                return 200, {"Content-Length": "100"}, b'{'
            return 200, {}, receipts[-1]
        if path == "/query":
            if self.phase == "query-delay":
                return 200, {}, {"receipts": []}
            receipts = self.state.get("receipts", [])
            wanted = {k: v[0] for k, v in q.items()}
            return 200, {}, {"receipts": [r for r in receipts if all(r.get(k) == v for k,v in wanted.items())]}
        return 404, {}, {"error": "页面不存在"}
