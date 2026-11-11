"""测试共享工具：只通过真实 HTTP 操作本地门户（含运维入口的故障注入）。

RPA 代码本身不导入本模块，也不接触 Source 内部函数；这里的 control/calls
仅用于测试编排故障场景。
"""
from __future__ import annotations

import json
import urllib.request
from urllib.error import URLError


def control(base_url: str, phase: str, state: dict | None = None) -> dict:
    payload = json.dumps({"phase": phase, "state": state or {}}).encode()
    req = urllib.request.Request(
        base_url + "/__control",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


def calls(base_url: str) -> list:
    with urllib.request.urlopen(base_url + "/__calls", timeout=5) as resp:
        return json.loads(resp.read().decode())


def count_submits(base_url: str) -> int:
    return sum(1 for item in calls(base_url)
               if item["method"] == "POST" and item["path"] == "/submit")


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def url_text(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.read().decode()
    except URLError:
        return ""
