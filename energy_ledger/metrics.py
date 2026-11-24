"""指标口径、可发布条件与园区汇总。

这里不写数据库，只接收事务性存储；函数返回普通字典，供 CLI、发布快照和
差异比较复用同一套计算。

关键约束：

* 电量按“计量点”归集，计量点只能通过已确认的有效期映射落到产线；
* 产量按来源行号去重后归集，口径必须是生产人员确认的合格品；
* 因子必须在覆盖该月的版本中取最高修订，不借用其他期间；
* 任一条件不满足，该产线不可比较，不生成指标值。
"""
from __future__ import annotations

import json
from typing import Any

from .util import day_after, day_before, normalize_scope, period_end, period_start, version_key

READY = "ready"
BLOCKED = "blocked"


def _issue(code: str, message: str, **evidence: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "evidence": evidence}


def _warning(code: str, message: str, **evidence: Any) -> dict[str, Any]:
    return {"code": code, "message": message, "evidence": evidence}


class PeriodContext:
    """一个月份内的最新观测、有效映射和口径确认。

    计量点在同一月可能有多条读数（例如抄表区间、换表前后分段）。版本按
    “计量点 + 读数号”分别判定，再汇总该计量点当月全部读数；这样一次更正只
    替换对应的那条读数，不会丢掉同月其他区间。
    """

    def __init__(self, store, period: str):
        self.store = store
        self.period = period
        raw_meters = [
            dict(row)
            for row in store.all(
                "SELECT * FROM meter_observations WHERE period = ? ORDER BY id", (period,)
            )
        ]
        by_reading: dict[tuple[str, str], dict[str, Any]] = {}
        ambiguous: set[str] = set()
        for row in raw_meters:
            key = (str(row["meter"]), str(row["reading"]))
            current = by_reading.get(key)
            if current is None:
                by_reading[key] = row
                continue
            if version_key(row["version"]) > version_key(current["version"]):
                by_reading[key] = row
            elif version_key(row["version"]) == version_key(current["version"]):
                if row["payload_hash"] != current["payload_hash"]:
                    ambiguous.add(str(row["meter"]))
                by_reading[key] = row
        self.meter_readings = list(by_reading.values())
        self.meter_versions: dict[str, list[dict[str, Any]]] = {}
        for row in self.meter_readings:
            self.meter_versions.setdefault(str(row["meter"]), []).append(row)
        for rows in self.meter_versions.values():
            rows.sort(key=lambda row: str(row["reading"]))
        self.meter_ambiguous = ambiguous

        raw_production = [
            dict(row)
            for row in store.all(
                "SELECT * FROM production_observations WHERE period = ? ORDER BY id", (period,)
            )
        ]
        # 同一来源行号取“最高来源版本”；无版本时按入库顺序取最后一条。
        # 若最高版本存在多条内容不同的记录，记为歧义并在评估时阻断。
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in raw_production:
            grouped.setdefault(str(row["row_id"]), []).append(row)
        self.production: dict[str, dict[str, Any]] = {}
        self.production_ambiguous: set[str] = set()
        for key, rows in grouped.items():
            top = max(version_key(row["version"]) for row in rows)
            top_rows = [row for row in rows if version_key(row["version"]) == top]
            self.production[key] = top_rows[-1]
            has_version = any(row["version"] for row in top_rows)
            if has_version and len({row["payload_hash"] for row in top_rows}) > 1:
                self.production_ambiguous.add(key)

        mapping_rows: dict[str, list[dict[str, Any]]] = {}
        for row in store.all(
            "SELECT * FROM meter_mappings WHERE valid_from <= ? AND valid_to >= ? "
            "ORDER BY valid_from, id",
            (period_end(period), period_start(period)),
        ):
            mapping_rows.setdefault(str(row["meter"]), []).append(dict(row))
        self.mapping_rows = mapping_rows
        # 同一计量点在同一个月份只允许一条有效映射；取 valid_from 最晚的一条，
        # 多于一条时由 ``_evaluate_meters`` 明确阻断。
        self.mappings = {
            meter: rows[-1] for meter, rows in mapping_rows.items()
        }

        # 产量口径必须整月有效。月中切换口径无法用月度合格产量拆分，必须阻断，
        # 而不是挑一条“看起来相似”的口径套用整月。
        scope_rows: dict[str, list[dict[str, Any]]] = {}
        scope_partial: dict[str, list[dict[str, Any]]] = {}
        for row in store.all(
            "SELECT * FROM production_scopes WHERE valid_from <= ? AND valid_to >= ? "
            "ORDER BY valid_from, id",
            (period_end(period), period_start(period)),
        ):
            item = dict(row)
            if item["valid_from"] <= period_start(period) and item["valid_to"] >= period_end(period):
                scope_rows.setdefault(str(item["line"]), []).append(item)
            else:
                scope_partial.setdefault(str(item["line"]), []).append(item)
        self.scope_rows = scope_rows
        self.scope_partial = scope_partial
        self.scopes = {line: rows[-1] for line, rows in scope_rows.items()}
        selection, series = resolve_factor_selection(store, period)
        self.factor_selection = selection
        self.factor_series = series

    def lines(self) -> list[str]:
        names: set[str] = set()
        for rows in self.meter_versions.values():
            for observation in rows:
                names.add(str(observation["line"]))
        for row in self.production.values():
            names.add(str(row["line"]))
        for mapping in self.mappings.values():
            names.add(str(mapping["line"]))
        names.update(self.scopes)
        names.update(self.scope_partial)
        return sorted(names)

    def meters_for_line(self, line: str) -> list[dict[str, Any]]:
        """观测产线或确认映射任一指向该产线的计量点都纳入评估。

        返回该计量点当月全部读数（已按读数号取最高版本）。这样映射与观测产线
        不一致时两侧都会因 ``meter_line_mismatch`` 被阻断，不会因为“看起来相近”
        而把电量悄悄归到另一条产线。
        """
        chosen: list[dict[str, Any]] = []
        for meter, rows in self.meter_versions.items():
            mapping = self.mappings.get(meter)
            observation_lines = {str(row["line"]) for row in rows}
            mapping_line = None if mapping is None else str(mapping["line"])
            if line in observation_lines or mapping_line == line:
                chosen.extend(rows)
        return sorted(chosen, key=lambda row: (row["meter"], str(row["reading"])))

    def production_for_line(self, line: str) -> list[dict[str, Any]]:
        return sorted(
            (row for row in self.production.values() if str(row["line"]) == line),
            key=lambda row: str(row["row_id"]),
        )


def resolve_factor_selection(store, period: str) -> tuple[dict[str, str] | None, list[dict[str, Any]]]:
    """返回(选择的因子系列, 该月可用系列)。

    系列由 ``source`` 与可选 ``factor_key`` 共同标识；同一系列的追溯修订由
    ``revision`` 区分。可用系列指有效期间整月覆盖该月的系列，没有覆盖该月的
    因子不会被“借”来用。选择可以是月份专用，也可以是全局默认。
    """
    series = [
        dict(row)
        for row in store.all(
            "SELECT DISTINCT source, factor_key FROM factor_observations "
            "WHERE effective_from <= ? AND effective_to >= ? ORDER BY source, factor_key",
            (period_start(period), period_end(period)),
        )
    ]
    raw = store.setting(f"factor_selection:{period}") or store.setting("factor_selection")
    if raw:
        try:
            chosen = json.loads(raw)
        except json.JSONDecodeError:
            return None, series
        return {
            "source": str(chosen.get("source", "")),
            "factor_key": str(chosen.get("factor_key", "")),
        }, series
    if len(series) == 1:
        return series[0], series
    return None, series


def factor_versions_for(store, source: str, factor_key: str) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store.all(
            "SELECT * FROM factor_observations WHERE source = ? AND factor_key = ? "
            "ORDER BY effective_from, revision, id",
            (source, factor_key),
        )
    ]


def select_factor_version(store, source: str, factor_key: str, period: str) -> dict[str, Any] | None:
    """在整月有效覆盖该期间的版本中取最高修订；不借用其他期间的因子。

    要求 ``effective_from <= 月初`` 且 ``effective_to >= 月末``，避免把月中生效的
    修订按整月直接套用。
    """
    rows = store.all(
        "SELECT * FROM factor_observations WHERE source = ? AND factor_key = ? "
        "AND effective_from <= ? AND effective_to >= ? ORDER BY revision DESC, id DESC",
        (source, factor_key, period_start(period), period_end(period)),
    )
    return dict(rows[0]) if rows else None


def conflicting_top_revisions(store, source: str, factor_key: str, period: str) -> list[dict[str, Any]]:
    """同一最高修订若有多条覆盖该月且数值不同，返回这些冲突记录。"""
    rows = [
        dict(row)
        for row in store.all(
            "SELECT * FROM factor_observations WHERE source = ? AND factor_key = ? "
            "AND effective_from <= ? AND effective_to >= ? ORDER BY revision DESC, id DESC",
            (source, factor_key, period_start(period), period_end(period)),
        )
    ]
    if not rows:
        return []
    top = int(rows[0]["revision"])
    same = [row for row in rows if int(row["revision"]) == top]
    if len({row["value"] for row in same}) > 1:
        return same
    return []


def _series_label(series: dict[str, Any]) -> str:
    key = series.get("factor_key")
    return str(series.get("source")) if not key else f"{series.get('source')}/{key}"


def _coverage_gap(context: PeriodContext, line: str) -> tuple[str, str] | None:
    """检查该产线的计量点映射是否连续覆盖整月。

    换表当月允许新旧计量点分段覆盖，但必须首尾相接覆盖月初到月末；若只覆盖
    部分日期，月度电量就无法按月归集，必须阻断而不是按“相近产线”或整月读数处理。
    """
    intervals: list[tuple[str, str]] = []
    for rows in context.mapping_rows.values():
        for row in rows:
            if str(row["line"]) != line:
                continue
            start = max(str(row["valid_from"]), period_start(context.period))
            end = min(str(row["valid_to"]), period_end(context.period))
            if start <= end:
                intervals.append((start, end))
    if not intervals:
        return None  # 由 meter_unmapped 报告
    intervals.sort()
    cursor = period_start(context.period)
    month_end = period_end(context.period)
    for start, end in intervals:
        if end < cursor:
            continue
        if start > cursor:
            return cursor, day_before(start)
        cursor = max(cursor, day_after(end))
        if cursor > month_end:
            return None
    if cursor <= month_end:
        return cursor, month_end
    return None


def _evaluate_meters(
    context: PeriodContext, line: str
) -> tuple[float | None, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    observations = context.meters_for_line(line)
    energy: float | None = 0.0
    evidence: list[dict[str, Any]] = []
    if not observations:
        issues.append(_issue("meter_no_observations", f"{line} 在 {context.period} 没有电量观测"))
        return None, issues, warnings, evidence

    # 先按计量点判定映射，再汇总该计量点当月全部读数；读数缺失会让整个计量点阻断。
    by_meter: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        by_meter.setdefault(str(observation["meter"]), []).append(observation)

    for meter, readings in sorted(by_meter.items()):
        mapping = context.mappings.get(meter)
        mapping_ok = True
        if meter in context.meter_ambiguous:
            issues.append(
                _issue(
                    "meter_version_ambiguous",
                    f"计量点 {meter} 在 {context.period} 的同一读数号存在多条不同内容的最高版本，"
                    "无法判断哪条有效",
                    meter=meter,
                )
            )
            mapping_ok = False
        if len(context.mapping_rows.get(meter, [])) > 1:
            issues.append(
                _issue(
                    "meter_mapping_overlap",
                    f"计量点 {meter} 在 {context.period} 有多条同时生效的映射，无法判断归集产线",
                    meter=meter,
                    mapping_ids=[row["id"] for row in context.mapping_rows[meter]],
                )
            )
            mapping_ok = False
        if mapping is None:
            issues.append(
                _issue(
                    "meter_unmapped",
                    f"计量点 {meter} 在 {context.period} 缺少已确认的产线映射，电量不得分摊给任何产线",
                    meter=meter,
                    observation_ids=[row["id"] for row in readings],
                )
            )
            mapping_ok = False
        else:
            if mapping["status"] == "pending":
                issues.append(
                    _issue(
                        "meter_mapping_pending",
                        f"计量点 {meter} 的产线映射待核，电量不参与计算",
                        meter=meter,
                        mapping_id=mapping["id"],
                    )
                )
                mapping_ok = False
            elif mapping["status"] == "partial":
                issues.append(
                    _issue(
                        "meter_mapping_partial",
                        f"计量点 {meter} 与 {line} 仅部分对应，不能按月全额归集",
                        meter=meter,
                        mapping_id=mapping["id"],
                    )
                )
                mapping_ok = False
            mismatched = sorted({str(row["line"]) for row in readings} - {str(mapping["line"])})
            if mismatched:
                issues.append(
                    _issue(
                        "meter_line_mismatch",
                        f"计量点 {meter} 的观测产线是 {', '.join(mismatched)}，"
                        f"但确认映射指向 {mapping['line']}",
                        meter=meter,
                        observation_ids=[row["id"] for row in readings],
                        mapping_id=mapping["id"],
                    )
                )
                mapping_ok = False
            if not (
                mapping["valid_from"] <= period_start(context.period)
                and mapping["valid_to"] >= period_end(context.period)
            ):
                # 换表期间计量点只对应部分天数属于正常情况；但必须提示核对读数是否
                # 也只覆盖该区间，避免把整月读数记到部分区间。
                warnings.append(
                    _warning(
                        "meter_mapping_partial_month",
                        f"计量点 {meter} 在 {context.period} 的映射只覆盖部分日期"
                        f"（{mapping['valid_from']}..{mapping['valid_to']}），请核对电表读数是否同区间",
                        meter=meter,
                        valid_from=mapping["valid_from"],
                        valid_to=mapping["valid_to"],
                    )
                )

        for observation in readings:
            item: dict[str, Any] = {
                "meter": meter,
                "observation_id": observation["id"],
                "reading": observation["reading"],
                "version": observation["version"],
                "batch_id": observation["batch_id"],
                "fetched_at": observation["fetched_at"],
                "kwh": observation["kwh"],
                "mapping": None
                if mapping is None
                else {
                    "id": mapping["id"],
                    "line": mapping["line"],
                    "status": mapping["status"],
                    "valid_from": mapping["valid_from"],
                    "valid_to": mapping["valid_to"],
                    "replaces_meter": mapping["replaces_meter"],
                    "decided_by": mapping["decided_by"],
                    "decided_at": mapping["decided_at"],
                },
            }
            evidence.append(item)
            if observation["kwh"] is None:
                issues.append(
                    _issue(
                        "meter_reading_missing",
                        f"计量点 {meter} 的读数 {observation['reading']} 在 {context.period} 的电量为空，"
                        "不能用其他期间填补",
                        meter=meter,
                        observation_id=observation["id"],
                    )
                )
                energy = None
            elif energy is not None and mapping_ok:
                energy += float(observation["kwh"])
        if not mapping_ok:
            energy = None

    meter_count = len(by_meter)
    # 映射有效但整月没有任何读数：覆盖检查会误以为区间完整，必须单独阻断。
    for meter, rows in sorted(context.mapping_rows.items()):
        if meter in by_meter:
            continue
        if not any(str(row["line"]) == line for row in rows):
            continue
        issues.append(
            _issue(
                "meter_observation_missing",
                f"计量点 {meter} 在 {context.period} 有有效映射但没有读数，月度电量不完整，"
                "不能用其他期间填补",
                meter=meter,
                mapping_ids=[row["id"] for row in rows],
            )
        )
        energy = None
    if meter_count > 1:
        warnings.append(
            _warning(
                "multiple_meters_same_line",
                f"{line} 在同月有 {meter_count} 个计量点参与归集；若存在整月重复计量请复核映射",
                meters=sorted(by_meter),
            )
        )
    if energy is not None:
        gap = _coverage_gap(context, line)
        if gap is not None:
            issues.append(
                _issue(
                    "meter_mapping_gap",
                    f"{line} 的计量点映射没有连续覆盖 {context.period} 的 {gap[0]}..{gap[1]}，"
                    "月度电量不完整，不能按整月计算",
                    gap_from=gap[0],
                    gap_to=gap[1],
                )
            )
            energy = None
    return energy, issues, warnings, evidence


def _evaluate_production(
    context: PeriodContext, line: str
) -> tuple[float | None, list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    rows = context.production_for_line(line)
    evidence = [
        {
            "observation_id": row["id"],
            "row_id": row["row_id"],
            "version": row["version"],
            "good_units": row["good_units"],
            "scope": row["scope"],
            "scope_raw": row["scope_raw"],
            "batch_id": row["batch_id"],
            "fetched_at": row["fetched_at"],
        }
        for row in rows
    ]
    units: float | None = 0.0
    if not rows:
        issues.append(_issue("production_missing", f"{line} 在 {context.period} 缺少合格产量"))
        return None, issues, warnings, evidence

    for row in rows:
        if str(row["row_id"]) in context.production_ambiguous:
            issues.append(
                _issue(
                    "production_version_ambiguous",
                    f"{line} 的来源行 {row['row_id']} 最高版本有多条不同内容，无法判断哪条有效",
                    row_id=row["row_id"],
                    version=row["version"],
                )
            )
            units = None

    scopes = sorted({normalize_scope(row["scope"]) or "unknown" for row in rows})
    if any(scope.startswith("unknown") for scope in scopes):
        issues.append(
            _issue(
                "production_unknown_scope",
                f"{line} 的产量口径无法识别：{', '.join(scopes)}",
                scopes=scopes,
            )
        )
        units = None
    if "includes-rework" in scopes:
        issues.append(
            _issue(
                "production_scope_not_final",
                f"{line} 的产量含返工品，不能作为合格品单位能耗的分母",
                observation_ids=[
                    row["id"] for row in rows if normalize_scope(row["scope"]) == "includes-rework"
                ],
            )
        )
        units = None

    for row in rows:
        if row["good_units"] is None:
            issues.append(
                _issue(
                    "production_missing",
                    f"{line} 的来源行 {row['row_id']} 合格产量为空，不能用其他期间填补",
                    row_id=row["row_id"],
                    observation_id=row["id"],
                )
            )
            units = None
        elif float(row["good_units"]) <= 0:
            issues.append(
                _issue(
                    "production_zero",
                    f"{line} 的来源行 {row['row_id']} 合格产量不为正，无法计算单位能耗",
                    row_id=row["row_id"],
                    observation_id=row["id"],
                )
            )
            units = None
        elif units is not None:
            units += float(row["good_units"])

    confirmation = context.scopes.get(line)
    if confirmation is None:
        if line in context.scope_partial:
            issues.append(
                _issue(
                    "production_scope_partial_period",
                    f"{line} 的产量口径确认只覆盖 {context.period} 的一部分；"
                    "月度合格产量无法按口径拆分，不能用其他期间或相近口径替代",
                    confirmation_ids=[row["id"] for row in context.scope_partial[line]],
                )
            )
        else:
            issues.append(
                _issue(
                    "production_scope_unconfirmed",
                    f"{line} 在 {context.period} 的产量口径尚未由生产人员确认",
                )
            )
        units = None
    else:
        if len(context.scope_rows.get(line, [])) > 1:
            issues.append(
                _issue(
                    "production_scope_overlap",
                    f"{line} 在 {context.period} 有多条同时生效的口径确认，无法判断适用哪条",
                    confirmation_ids=[row["id"] for row in context.scope_rows[line]],
                )
            )
            units = None
        if confirmation["status"] != "confirmed":
            issues.append(
                _issue(
                    "production_scope_unconfirmed",
                    f"{line} 的产量口径确认仍为待核状态",
                    confirmation_id=confirmation["id"],
                )
            )
            units = None
        if confirmation["scope"] != "final":
            issues.append(
                _issue(
                    "production_scope_not_final",
                    f"{line} 确认的统计口径是 {confirmation['scope']}，不是合格品 final",
                    confirmation_id=confirmation["id"],
                )
            )
            units = None
        else:
            mismatched = [row for row in rows if normalize_scope(row["scope"]) != "final"]
            if mismatched:
                issues.append(
                    _issue(
                        "production_scope_mismatch",
                        f"{line} 的来源产量口径与确认的 final 口径不一致",
                        confirmation_id=confirmation["id"],
                        observation_ids=[row["id"] for row in mismatched],
                    )
                )
                units = None

    if len(rows) > 1:
        warnings.append(
            _warning(
                "production_multiple_rows",
                f"{line} 在同月有 {len(rows)} 个产量行，已按来源行号求和",
                row_ids=[str(row["row_id"]) for row in rows],
            )
        )
    return units, issues, warnings, evidence


def _evaluate_factor(context: PeriodContext) -> tuple[float | None, dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    selection = context.factor_selection
    if selection is None:
        if not context.factor_series:
            issues.append(
                _issue("factor_missing", f"{context.period} 没有可用的公开折算因子")
            )
        else:
            issues.append(
                _issue(
                    "factor_ambiguous",
                    f"{context.period} 存在多个因子来源，尚未指定适用的因子系列："
                    + "、".join(_series_label(s) for s in context.factor_series),
                    series=context.factor_series,
                )
            )
        return None, None, issues, warnings

    selected = select_factor_version(
        context.store, selection["source"], selection["factor_key"], context.period
    )
    versions = factor_versions_for(context.store, selection["source"], selection["factor_key"])
    evidence: dict[str, Any] = {
        "selection": selection,
        "versions": [
            {
                "id": row["id"],
                "revision": row["revision"],
                "value": row["value"],
                "effective_from": row["effective_from"],
                "effective_to": row["effective_to"],
                "batch_id": row["batch_id"],
                "fetched_at": row["fetched_at"],
            }
            for row in versions
        ],
        "applied": None,
    }
    value: float | None = None
    if selected is None:
        issues.append(
            _issue(
                "factor_no_covering_revision",
                f"因子 {_series_label(selection)} 没有整月覆盖 {context.period} 的适用版本，"
                "不能用其他期间的因子替代",
                series=selection,
            )
        )
        return None, evidence, issues, warnings

    evidence["applied"] = {
        "id": selected["id"],
        "factor": selected["factor"],
        "revision": selected["revision"],
        "value": selected["value"],
        "effective_from": selected["effective_from"],
        "effective_to": selected["effective_to"],
    }
    if selected["value"] is None or float(selected["value"]) <= 0:
        issues.append(
            _issue(
                "factor_value_missing",
                f"因子修订 r{selected['revision']} 的数值为空或不为正；不会回退到旧修订",
                factor_observation_id=selected["id"],
                revision=selected["revision"],
            )
        )
        return None, evidence, issues, warnings

    conflicts = conflicting_top_revisions(
        context.store, selection["source"], selection["factor_key"], context.period
    )
    if conflicts:
        issues.append(
            _issue(
                "factor_revision_ambiguous",
                f"因子 {_series_label(selection)} 的最高修订 r{conflicts[0]['revision']} "
                f"在 {context.period} 有 {len(conflicts)} 条数值不同的记录，无法判断哪条有效",
                factor_observation_ids=[row["id"] for row in conflicts],
            )
        )
        return None, evidence, issues, warnings

    value = float(selected["value"])
    return value, evidence, issues, warnings

def evaluate_line(context: PeriodContext, line: str) -> dict[str, Any]:
    energy, meter_issues, meter_warnings, meter_evidence = _evaluate_meters(context, line)
    units, production_issues, production_warnings, production_evidence = _evaluate_production(context, line)
    factor, factor_evidence, factor_issues, factor_warnings = _evaluate_factor(context)
    issues = meter_issues + production_issues + factor_issues
    warnings = meter_warnings + production_warnings + factor_warnings
    ready = not issues and energy is not None and units is not None and factor is not None

    metric = None
    if ready:
        converted = energy * factor
        metric = {
            "energy_kwh": energy,
            "good_units": units,
            "factor_value": factor,
            "converted_energy": converted,
            "kwh_per_unit": energy / units,
            "converted_per_unit": converted / units,
        }

    confirmation = context.scopes.get(line)
    return {
        "line": line,
        "period": context.period,
        "status": READY if ready else BLOCKED,
        "issues": issues,
        "warnings": warnings,
        "metric": metric,
        "evidence": {
            "meters": meter_evidence,
            "production": production_evidence,
            "scope_confirmation": None
            if confirmation is None
            else {
                "id": confirmation["id"],
                "status": confirmation["status"],
                "scope": confirmation["scope"],
                "product": confirmation["product"],
                "process_version": confirmation["process_version"],
                "valid_from": confirmation["valid_from"],
                "valid_to": confirmation["valid_to"],
                "decided_by": confirmation["decided_by"],
                "decided_at": confirmation["decided_at"],
            },
            "factor": factor_evidence,
        },
    }


def compute_period(store, period: str) -> dict[str, Any]:
    """计算草稿；只有满足全部可发布条件的产线才产生指标。"""
    context = PeriodContext(store, period)
    evaluated = [evaluate_line(context, line) for line in context.lines()]

    ready = [item for item in evaluated if item["status"] == READY]
    blocked = [item for item in evaluated if item["status"] != READY]
    ready.sort(key=lambda item: (item["metric"]["converted_per_unit"], item["line"]))
    ranking = [
        {
            "rank": index + 1,
            "line": item["line"],
            "converted_per_unit": item["metric"]["converted_per_unit"],
            "kwh_per_unit": item["metric"]["kwh_per_unit"],
        }
        for index, item in enumerate(ready)
    ]

    total_kwh = sum(item["metric"]["energy_kwh"] for item in ready)
    total_units = sum(item["metric"]["good_units"] for item in ready)
    total_converted = sum(item["metric"]["converted_energy"] for item in ready)
    park = {
        "ready_lines": len(ready),
        "blocked_lines": len(blocked),
        "total_energy_kwh": total_kwh if ready else None,
        "total_good_units": total_units if ready else None,
        "converted_per_unit": (total_converted / total_units) if ready and total_units else None,
        "kwh_per_unit": (total_kwh / total_units) if ready and total_units else None,
    }

    return {
        "period": period,
        "factor_selection": context.factor_selection,
        "factor_series_available": context.factor_series,
        "lines": evaluated,
        "ranking": ranking,
        "comparable_lines": [item["line"] for item in ready],
        "incomparable": [
            {"line": item["line"], "reasons": [issue["message"] for issue in item["issues"]]}
            for item in blocked
        ],
        "park_summary": park,
    }
