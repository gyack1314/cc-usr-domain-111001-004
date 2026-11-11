"""脱敏与证据留存。

日志和证据文件均不得出现口令或 session cookie。脱敏在写入任何文件前统一完成，
方便工作人员事后接手，又不泄露合成账号的会话材料。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# password=xxx（URL 编码表单风格）与 session=xxx（Cookie 风格）
_SECRET_PATTERNS = (
    re.compile(r"(?i)(password\s*[=:]\s*)[^;&\s\"']+"),
    re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*[^;\n]*session=)[^;\s\"']+"),
    re.compile(r"(?i)(session=)[^;\s\"']+"),
)

MASK = "***REDACTED***"


def redact(text: str) -> str:
    """对任意文本中的口令与 session 令牌打码。"""
    if text is None:
        return ""
    out = str(text)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(lambda m: m.group(1) + MASK, out)
    return out


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # 先完成 % 渲染，再对最终文本统一脱敏，并清空 args 避免二次格式化。
        rendered = record.getMessage()
        record.msg = redact(rendered)
        record.args = ()
        return True


def configure_logging(verbose: bool = False, log_file: str | Path | None = None):
    level = logging.DEBUG if verbose else logging.INFO
    logger = logging.getLogger("rpa")
    logger.setLevel(level)
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.addFilter(RedactingFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(handler)
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.addFilter(RedactingFilter())
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        logger.addHandler(file_handler)
    logger.propagate = False
    return logger


class EvidenceStore:
    """把每次关键交互的脱敏证据写入证据目录。"""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        task_key: str,
        kind: str,
        detail: dict[str, Any],
        clock=None,
    ) -> Path:
        now = clock.now(timezone.utc) if clock else datetime.now(timezone.utc)
        stamp = now.strftime("%Y%m%dT%H%M%S%fZ")
        safe_key = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_key)
        path = self.directory / f"{stamp}_{safe_key}_{kind}.json"
        payload = {
            "at": now.isoformat(),
            "task_key": task_key,
            "kind": kind,
            "detail": detail,
        }
        # 先序列化再脱敏，避免口令/Cookie 以任何形式落盘。
        text = redact(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        )
        path.write_text(text, encoding="utf-8")
        return path
