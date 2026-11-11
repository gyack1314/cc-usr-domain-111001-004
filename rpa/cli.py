"""命令行入口：预览、确认批次、逐项状态、核对与人工补录验证。

预览阶段只读取本地资料，绝不访问门户；带 --confirm 的批次才会调用提交页。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import portal as portal_mod
from . import store as store_mod
from .config import Config, ConfigError, load_config
from .engine import Engine
from .model import validate_material
from .security import EvidenceStore, configure_logging


# ---- 资料读取（纯本地）---------------------------------------------------
def load_materials(path: Path):
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("资料文件必须是 JSON 数组")
    materials, invalid = [], []
    for index, item in enumerate(raw):
        material, reason = validate_material(item)
        if material is None:
            invalid.append({"index": index, "reason": reason, "raw": item})
        else:
            materials.append(material)
    return materials, invalid


def build_engine(config: Config, worker_id: str | None = None) -> Engine:
    store = store_mod.TaskStore(config.db_path, lease_seconds=config.lease_seconds)
    evidence = EvidenceStore(config.evidence_dir)

    def portal_factory(organization: str) -> portal_mod.PortalClient:
        account = config.password_for(organization)
        return portal_mod.PortalClient(
            config.portal_base_url,
            account=account.account,
            password=account.password,
            timeout=config.timeout,
        )

    return Engine(store, portal_factory, evidence, worker_id=worker_id)


def _import_materials(engine: Engine, materials, invalid):
    created, existing = [], []
    for material in materials:
        task_key, is_created = engine.store.upsert_material(material)
        (created if is_created else existing).append(task_key)
    for item in invalid:
        key = "{}|{}#{}".format(
            item["raw"].get("organization", "UNKNOWN"),
            item["raw"].get("activity_id", "UNKNOWN"),
            item["index"],
        )
        engine.store.record_attempt(
            key, "import", "invalid",
            {"reason": item["reason"], "raw": item["raw"]})
    return created, existing


# ---- 子命令 ---------------------------------------------------------------
def cmd_preview(args) -> int:
    config = load_config(args.config)
    materials, invalid = load_materials(Path(args.submissions))
    lines = []
    for material in materials:
        lines.append({
            "natural_key": material.key,
            "fields": material.form_fields(),
            "content_hash": material.content_digest,
        })
    output = {
        "mode": "PREVIEW_ONLY_NO_HTTP",
        "portal_calls": 0,
        "task_count": len(materials),
        "tasks": lines,
        "invalid": invalid,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if invalid:
        print(f"注意：{len(invalid)} 条资料校验失败，未纳入批次", file=sys.stderr)
    return 0


def cmd_run(args) -> int:
    config = load_config(args.config)
    configure_logging(args.verbose, config.log_file)
    engine = build_engine(config, worker_id=args.worker_id)
    materials, invalid = load_materials(Path(args.submissions))
    created, existing = _import_materials(engine, materials, invalid)
    print(f"导入：新增 {len(created)} 条，已登记 {len(existing)} 条，"
          f"无效 {len(invalid)} 条")
    if not args.confirm:
        print("预览保护：未提供 --confirm，不调用提交页。"
              "请先使用 preview 查看字段与自然键。")
        _print_status(engine)
        return 0

    totals = engine.run_until_idle(max_rounds=args.max_rounds)
    print(f"批次完成：{totals}")
    _print_status(engine)
    return 0


def cmd_status(args) -> int:
    config = load_config(args.config)
    store = store_mod.TaskStore(config.db_path, lease_seconds=config.lease_seconds)
    tasks = store.list_tasks()
    if args.task_key:
        task = next((t for t in tasks if t["task_key"] == args.task_key), None)
        if task is None:
            print(f"未找到任务 {args.task_key}", file=sys.stderr)
            return 2
        output = dict(task)
        output["material"] = json.loads(task["material_json"])
        output["attempts"] = store.list_attempts(args.task_key)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    _print_task_table(tasks)
    print("汇总:", json.dumps(store.counts(), ensure_ascii=False))
    return 0


def cmd_reconcile(args) -> int:
    config = load_config(args.config)
    engine = build_engine(config, worker_id=args.worker_id)
    configure_logging(args.verbose, config.log_file)
    rounds = 0
    while rounds < args.max_rounds:
        task = engine.store.claim_reconcile(engine.worker_id)
        if task is None:
            break
        outcome = engine.reconcile(task["task_key"])
        rounds += 1
        print(f"{task['task_key']} -> {outcome}")
    if rounds == 0:
        print("当前没有待核实任务。")
    _print_status(engine)
    return 0


def cmd_verify_receipt(args) -> int:
    config = load_config(args.config)
    engine = build_engine(config, worker_id=args.worker_id)
    configure_logging(args.verbose, config.log_file)
    if engine.store.get_task(args.task_key) is None:
        print(f"未找到任务 {args.task_key}（自然键格式为 组织代码|活动编号）",
              file=sys.stderr)
        return 2
    result = engine.verify_manual_receipt(args.task_key, args.receipt)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("result_code") == "VERIFIED" else 1


def cmd_reset(args) -> int:
    config = load_config(args.config)
    store = store_mod.TaskStore(config.db_path, lease_seconds=config.lease_seconds)
    ok = store.reset_for_retry(args.task_key)
    if not ok:
        print("只能对门户明确拒绝（REJECTED）的任务放行重试", file=sys.stderr)
        return 1
    print(f"任务 {args.task_key} 已回到 PENDING，可在修正资料后重新运行。")
    return 0


# ---- 输出 -----------------------------------------------------------------
def _print_status(engine: Engine) -> None:
    _print_task_table(engine.store.list_tasks())
    print("汇总:", json.dumps(engine.store.counts(), ensure_ascii=False))


def _print_task_table(tasks) -> None:
    for task in tasks:
        print(
            f"- {task['task_key']:<20} {task['status']:<15} "
            f"回执={task['last_receipt'] or '-'} "
            f"原因={task['last_reason'] or '-'}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rpa",
        description="社区演练资料表单 RPA（仅标准库，仅访问门户四个业务页面）")
    parser.add_argument("--config", required=True, help="运行配置 JSON 路径")
    parser.add_argument("--verbose", action="store_true", help="调试日志（仍脱敏）")
    parser.add_argument("--worker-id", help="工作进程标识，默认自动生成")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("preview", help="只预览字段与自然键，不发起任何 HTTP 请求")
    p.add_argument("submissions")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("run", help="导入资料并处理；不加 --confirm 只登记不提交")
    p.add_argument("submissions")
    p.add_argument("--confirm", action="store_true",
                   help="确认批次，允许调用表单页与提交页")
    p.add_argument("--max-rounds", type=int, default=100)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("status", help="输出逐项状态、回执与尝试证据")
    p.add_argument("task_key", nargs="?")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("reconcile", help="对待核实任务执行查询页核对")
    p.add_argument("--max-rounds", type=int, default=50)
    p.set_defaults(func=cmd_reconcile)

    p = sub.add_parser("verify-receipt",
                       help="人工补录回执：回到门户验证组织与内容后才确认")
    p.add_argument("task_key")
    p.add_argument("receipt")
    p.set_defaults(func=cmd_verify_receipt)

    p = sub.add_parser("reset", help="对明确被拒任务修正后放行重试")
    p.add_argument("task_key")
    p.set_defaults(func=cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, ValueError) as exc:
        print(f"输入错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
