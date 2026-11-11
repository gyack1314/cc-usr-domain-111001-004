"""资料模型、自然键与内容哈希。

自然键 = (organization, activity_id)，门户允许同一自然键存在多条提交，
因此自然键不具备幂等性；内容确认使用两道哈希：

- summary_hash：门户协议规定的字段，UTF-8 摘要正文的 SHA-256，随表单一并提交；
- content_hash：本地对全部五个业务字段的规范化 SHA-256，用于与查询页回执比对，
  确认“内容相同”而不仅仅是摘要相同。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping

REQUIRED_FIELDS = (
    "organization",
    "activity_id",
    "date",
    "participants",
    "summary",
)
NATURAL_KEY_FIELDS = ("organization", "activity_id")


def summary_hash(summary: str) -> str:
    return hashlib.sha256(summary.encode("utf-8")).hexdigest()


def content_hash(material: Mapping[str, Any]) -> str:
    """对完整资料内容计算稳定哈希，作为本地“内容相同”的判据。"""
    payload = {key: material[key] for key in REQUIRED_FIELDS}
    blob = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def natural_key(material: Mapping[str, Any]) -> str:
    return "{}|{}".format(
        material["organization"], material["activity_id"]
    )


@dataclass(frozen=True)
class Material:
    organization: str
    activity_id: str
    date: str
    participants: int
    summary: str

    @property
    def key(self) -> str:
        return f"{self.organization}|{self.activity_id}"

    @property
    def summary_digest(self) -> str:
        return summary_hash(self.summary)

    @property
    def content_digest(self) -> str:
        return content_hash(self.as_dict())

    def as_dict(self) -> dict[str, Any]:
        return {
            "organization": self.organization,
            "activity_id": self.activity_id,
            "date": self.date,
            "participants": self.participants,
            "summary": self.summary,
        }

    def form_fields(self) -> dict[str, str]:
        """预览与提交共用的字段视图；预览阶段不会把它发往提交页。"""
        return {
            "organization": self.organization,
            "activity_id": self.activity_id,
            "date": self.date,
            "participants": str(self.participants),
            "summary_hash": self.summary_digest,
        }


def validate_material(raw: Mapping[str, Any]) -> tuple[Material | None, str | None]:
    """校验一条合成资料，返回 (Material, None) 或 (None, 原因)。"""
    missing = [key for key in REQUIRED_FIELDS if key not in raw or raw[key] == ""]
    if missing:
        return None, "缺少字段: " + ", ".join(missing)
    if not all(isinstance(raw[key], str) and raw[key].strip()
               for key in ("organization", "activity_id", "date", "summary")):
        return None, "字符串字段不能为空"
    try:
        participants = int(raw["participants"])
    except (TypeError, ValueError):
        return None, "参与人数必须是非负整数"
    if participants < 0:
        return None, "参与人数必须是非负整数"
    material = Material(
        organization=str(raw["organization"]).strip(),
        activity_id=str(raw["activity_id"]).strip(),
        date=str(raw["date"]).strip(),
        participants=participants,
        summary=str(raw["summary"]).strip(),
    )
    return material, None
