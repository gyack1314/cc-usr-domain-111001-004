"""运行配置。

账号为合成账号，通过配置文件传入；口令支持环境变量引用，避免硬编码。
日志与证据均不输出口令或 session cookie（见 rpa.security）。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AccountConfig:
    organization: str
    account: str
    password: str


@dataclass(frozen=True)
class Config:
    portal_base_url: str
    accounts: dict[str, AccountConfig]  # 按组织代码索引
    db_path: str
    evidence_dir: str
    log_file: str | None
    timeout: float
    lease_seconds: float

    def password_for(self, organization: str) -> AccountConfig:
        if organization not in self.accounts:
            raise ConfigError(
                f"配置中缺少组织 {organization} 的合成账号，无法登录门户")
        return self.accounts[organization]


def load_config(path: str | Path) -> Config:
    config_path = Path(path)
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    base_url = raw.get("portal_base_url")
    if not base_url or not isinstance(base_url, str):
        raise ConfigError("portal_base_url 必填")

    accounts: dict[str, AccountConfig] = {}
    for item in raw.get("accounts", []):
        organization = item.get("organization")
        account = item.get("account")
        password_env = item.get("password_env")
        password = item.get("password")
        if password_env:
            password = os.environ.get(password_env)
            if not password:
                raise ConfigError(
                    f"环境变量 {password_env} 未设置，无法取得合成账号口令")
        if not all([organization, account, password]):
            raise ConfigError(
                "每个账号需要 organization、account 与 password/password_env")
        if organization in accounts:
            raise ConfigError(f"组织 {organization} 配置了重复账号")
        accounts[organization] = AccountConfig(
            organization=organization, account=account, password=password)

    base_dir = config_path.parent
    def _resolve(p: str | None) -> str | None:
        if p is None:
            return None
        path = Path(p)
        return str(path if path.is_absolute() else (base_dir / path).resolve())

    return Config(
        portal_base_url=base_url.rstrip("/"),
        accounts=accounts,
        db_path=_resolve(raw.get("db_path", "state/tasks.sqlite3")),
        evidence_dir=_resolve(raw.get("evidence_dir", "state/evidence")),
        log_file=_resolve(raw.get("log_file")),
        timeout=float(raw.get("timeout_seconds", 5.0)),
        lease_seconds=float(raw.get("lease_seconds", 60.0)),
    )
