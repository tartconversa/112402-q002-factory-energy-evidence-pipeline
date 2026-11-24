"""命令行入口：``python -m energy_ledger ...``。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import LedgerError, ValidationError
from .ledger import Ledger
from .sources import build_source
from .util import canonical_json


def _load_config(args: argparse.Namespace) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if args.config:
        try:
            config.update(json.loads(Path(args.config).read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError(f"无法读取来源配置 {args.config}：{exc}") from exc
    if args.config_json:
        try:
            config.update(json.loads(args.config_json))
        except json.JSONDecodeError as exc:
            raise ValidationError(f"--config-json 不是合法 JSON：{exc}") from exc
    if getattr(args, "map_column", None):
        mapping = config.setdefault("source_columns", {})
        for pair in args.map_column:
            if "=" not in pair:
                raise ValidationError(f"--map-column 需要 来源列=标准字段，收到 {pair!r}")
            original, standard = pair.split("=", 1)
            if not original.strip() or not standard.strip():
                raise ValidationError(f"--map-column 两侧都不能为空，收到 {pair!r}")
            mapping[original.strip()] = standard.strip()
    if getattr(args, "path", None):
        config["path"] = args.path
    if getattr(args, "url", None):
        config["url"] = args.url
    if "kind" not in config:
        if args.url:
            config["kind"] = "http-json"
        elif args.path:
            suffix = Path(args.path).suffix.lower()
            config["kind"] = "html-table" if suffix in (".html", ".htm") else (
                "json-file" if suffix == ".json" else "csv"
            )
    if getattr(args, "page_size", None) is not None:
        config["page_size"] = args.page_size
    for flag, key in (
        ("period_from", "period_from"),
        ("period_to", "period_to"),
        ("line", "line"),
        ("token_env", "token_env"),
        ("records_path", "records_path"),
        ("next_path", "next_path"),
        ("total_path", "total_path"),
    ):
        value = getattr(args, flag, None)
        if value is not None:
            config[key] = value
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="energy-ledger",
        description="工厂能耗观测留痕、月度指标核定与修订影响分析",
    )
    parser.add_argument("--db", default="energy_ledger.db", help="SQLite 数据库路径")
    parser.add_argument("--actor", default="analyst", help="本次操作的操作人")
    parser.add_argument("--pretty", action="store_true", help="缩进输出 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="初始化数据库")

    fetch = sub.add_parser("fetch", help="新建采集批次并抓取")
    fetch.add_argument("source", choices=("meters", "production", "factors"))
    fetch.add_argument("--origin", required=True, help="来源标识，例如电表平台批次号")
    fetch.add_argument("--config", help="来源配置文件（JSON）")
    fetch.add_argument("--config-json", help="直接传入来源配置 JSON")
    fetch.add_argument("--path", help="CSV/JSON/HTML 文件路径")
    fetch.add_argument("--url", help="HTTP JSON 或公开页面 URL")
    fetch.add_argument("--page-size", type=int)
    fetch.add_argument("--period-from")
    fetch.add_argument("--period-to")
    fetch.add_argument("--line")
    fetch.add_argument("--token-env", help="授权令牌所在环境变量名（不要直接写令牌）")
    fetch.add_argument("--records-path")
    fetch.add_argument("--next-path")
    fetch.add_argument("--total-path")
    fetch.add_argument(
        "--map-column",
        action="append",
        metavar="来源列=标准字段",
        help="把非标准列名映射为标准字段，可重复",
    )
    fetch.add_argument("--pages", type=int, help="本次最多写入多少页；用于分批运行或演练中断")
    fetch.add_argument("--note")

    resume = sub.add_parser("resume", help="从已中断或未完成的批次继续")
    resume.add_argument("batch_id", type=int)
    resume.add_argument("--pages", type=int, help="本次最多写入多少页")

    batches = sub.add_parser("batches", help="查看采集批次")
    batches.add_argument("--batch-id", type=int)

    mapping = sub.add_parser("mapping", help="计量点与产线的有效期映射")
    mapping_sub = mapping.add_subparsers(dest="mapping_command", required=True)
    mapping_add = mapping_sub.add_parser("add", help="登记确认/部分对应/待核映射")
    mapping_add.add_argument("meter")
    mapping_add.add_argument("line")
    mapping_add.add_argument("--from", dest="valid_from", required=True)
    mapping_add.add_argument("--to", dest="valid_to", required=True)
    mapping_add.add_argument("--status", default="confirmed", choices=("confirmed", "partial", "pending"))
    mapping_add.add_argument("--replaces-meter", help="换表后，被替代的旧计量点")
    mapping_add.add_argument("--supersedes-id", type=int, help="取代某条历史确认，保留审计")
    mapping_add.add_argument("--note")
    mapping_add.add_argument("--by")
    mapping_list = mapping_sub.add_parser("list")
    mapping_list.add_argument("--meter")
    mapping_retract = mapping_sub.add_parser("retract", help="降级为待核，不删除历史")
    mapping_retract.add_argument("mapping_id", type=int)
    mapping_retract.add_argument("--reason")
    mapping_retract.add_argument("--by")

    scope = sub.add_parser("scope", help="产量统计口径确认")
    scope_sub = scope.add_subparsers(dest="scope_command", required=True)
    scope_add = scope_sub.add_parser("add")
    scope_add.add_argument("line")
    scope_add.add_argument("--from", dest="valid_from", required=True)
    scope_add.add_argument("--to", dest="valid_to", required=True)
    scope_add.add_argument("--scope", default="final", choices=("final", "includes-rework"))
    scope_add.add_argument("--product")
    scope_add.add_argument("--process-version")
    scope_add.add_argument("--status", default="confirmed", choices=("confirmed", "pending"))
    scope_add.add_argument("--supersedes-id", type=int)
    scope_add.add_argument("--note")
    scope_add.add_argument("--by")
    scope_list = scope_sub.add_parser("list")
    scope_list.add_argument("--line")

    factor = sub.add_parser("factor", help="公开折算因子")
    factor_sub = factor.add_subparsers(dest="factor_command", required=True)
    factor_select = factor_sub.add_parser("select", help="指定适用因子系列")
    factor_select.add_argument("--source", required=True)
    factor_select.add_argument("--factor-key", default="")
    factor_select.add_argument("--period", help="仅约束某月；省略则作为默认")
    factor_versions = factor_sub.add_parser("versions", help="查看因子修订历史")
    factor_versions.add_argument("--source")
    factor_versions.add_argument("--factor-key")

    compute = sub.add_parser("compute", help="计算某月草稿（未发布）")
    compute.add_argument("period")

    publish = sub.add_parser("publish", help="定版某月指标")
    publish.add_argument("period")
    publish.add_argument("--note")
    publish.add_argument("--allow-partial", action="store_true",
                         help="允许部分产线不可比，并在报告中保留原因")
    publish.add_argument("--by")

    releases = sub.add_parser("releases", help="列出发布记录")
    releases.add_argument("--period")
    release = sub.add_parser("release", help="查看/导出某次发布")
    release_sub = release.add_subparsers(dest="release_command", required=True)
    release_show = release_sub.add_parser("show")
    release_show.add_argument("period")
    release_show.add_argument("--revision", type=int)
    release_export = release_sub.add_parser("export")
    release_export.add_argument("period")
    release_export.add_argument("--revision", type=int)

    impact = sub.add_parser("impact", help="比较已发布快照与当前重算")
    impact.add_argument("period")

    trace = sub.add_parser("trace", help="查回指标口径与原始观测")
    trace.add_argument("period")
    trace.add_argument("line")

    compare = sub.add_parser("compare", help="同一产线两个月比较")
    compare.add_argument("line")
    compare.add_argument("--a", required=True, dest="period_a")
    compare.add_argument("--b", required=True, dest="period_b")

    observations = sub.add_parser("observations", help="查看某月全部原始观测")
    observations.add_argument("period")
    observations.add_argument("--line")

    audit = sub.add_parser("audit", help="查看操作审计")
    audit.add_argument("--limit", type=int, default=50)

    # 允许全局参数也写在子命令之后，例如 ``release show 2026-09 --pretty``。
    # 使用 SUPPRESS 默认值，未出现时不会覆盖主解析器上的值。
    def add_global_flags(parsers) -> None:
        for subparser in parsers:
            subparser.add_argument("--db", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
            subparser.add_argument("--actor", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
            subparser.add_argument("--pretty", action="store_true", default=argparse.SUPPRESS,
                                   help=argparse.SUPPRESS)
            nested = [
                action.choices for action in subparser._actions
                if isinstance(action, argparse._SubParsersAction)
            ]
            for choices in nested:
                add_global_flags(choices.values())

    add_global_flags(list(sub.choices.values()))

    return parser


def _dispatch(ledger: Ledger, args: argparse.Namespace) -> Any:
    command = args.command
    if command == "init":
        return {"database": ledger.storage.path, "schema_version": 1, "status": "ready"}

    if command == "fetch":
        config = _load_config(args)
        if not config:
            raise ValidationError("fetch 需要 --config、--config-json 或至少一个来源参数")
        # 先构建一次来源，尽早暴露配置错误，且不把令牌写进数据库。
        build_source(config)
        summary = ledger.fetch(
            args.source, args.origin, config=config, note=args.note, max_pages=args.pages
        )
        return summary

    if command == "resume":
        return ledger.run_batch(args.batch_id, max_pages=args.pages)

    if command == "batches":
        if args.batch_id:
            return ledger.batch_summary(args.batch_id)
        return ledger.list_batches()

    if command == "mapping":
        if args.mapping_command == "add":
            mapping_id = ledger.confirm_meter_mapping(
                args.meter, args.line, args.valid_from, args.valid_to,
                status=args.status, replaces_meter=args.replaces_meter,
                supersedes_id=args.supersedes_id, note=args.note, decided_by=args.by,
            )
            return {"mapping_id": mapping_id, "mappings": ledger.list_meter_mappings(args.meter)}
        if args.mapping_command == "list":
            return ledger.list_meter_mappings(args.meter)
        ledger.retract_meter_mapping(args.mapping_id, reason=args.reason, decided_by=args.by)
        return {"mapping_id": args.mapping_id, "status": "pending"}

    if command == "scope":
        if args.scope_command == "add":
            scope_id = ledger.confirm_production_scope(
                args.line, args.valid_from, args.valid_to, scope=args.scope,
                product=args.product, process_version=args.process_version,
                status=args.status, supersedes_id=args.supersedes_id,
                note=args.note, decided_by=args.by,
            )
            return {"scope_id": scope_id, "scopes": ledger.list_production_scopes(args.line)}
        return ledger.list_production_scopes(args.line)

    if command == "factor":
        if args.factor_command == "select":
            ledger.set_factor_selection(args.source, args.factor_key, period=args.period)
            return {"source": args.source, "factor_key": args.factor_key, "period": args.period}
        if args.source:
            if args.factor_key is not None:
                rows = ledger.storage.all(
                    "SELECT * FROM factor_observations WHERE source = ? AND factor_key = ? "
                    "ORDER BY effective_from, revision, id",
                    (args.source, args.factor_key),
                )
            else:
                rows = ledger.storage.all(
                    "SELECT * FROM factor_observations WHERE source = ? "
                    "ORDER BY effective_from, factor_key, revision, id",
                    (args.source,),
                )
            return [dict(row) for row in rows]
        return [
            dict(row) for row in ledger.storage.all(
                "SELECT * FROM factor_observations "
                "ORDER BY effective_from, source, factor_key, revision, id"
            )
        ]

    if command == "compute":
        return ledger.compute(args.period)

    if command == "publish":
        release = ledger.publish(
            args.period, note=args.note, allow_partial=args.allow_partial, published_by=args.by
        )
        return {
            "release": {
                "id": release["id"],
                "period": release["period"],
                "revision": release["revision"],
                "published_at": release["published_at"],
                "published_by": release["published_by"],
                "payload_hash": release["payload_hash"],
            },
            "draft": release["payload"]["draft"],
        }

    if command == "releases":
        return [
            {
                "id": release["id"],
                "period": release["period"],
                "revision": release["revision"],
                "published_at": release["published_at"],
                "published_by": release["published_by"],
                "payload_hash": release["payload_hash"],
                "note": release["note"],
            }
            for release in ledger.list_releases(args.period)
        ]

    if command == "release":
        if args.release_command == "export":
            return ledger.export_release(args.period, args.revision)
        return ledger.get_release(args.period, args.revision)

    if command == "impact":
        return ledger.impact(args.period)

    if command == "trace":
        return ledger.trace(args.period, args.line)

    if command == "compare":
        return ledger.compare_periods(args.line, args.period_a, args.period_b)

    if command == "observations":
        return ledger.list_observations(args.period, args.line)

    if command == "audit":
        return ledger.audit_log(args.limit)

    raise ValidationError(f"未知命令：{command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        with Ledger(args.db, actor=args.actor) as ledger:
            result = _dispatch(ledger, args)
    except LedgerError as exc:
        payload = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        if getattr(exc, "batch_id", None) is not None:
            payload["batch_id"] = exc.batch_id
        print(canonical_json(payload) if not args.pretty else json.dumps(payload, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.pretty else None, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
