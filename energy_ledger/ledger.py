"""业务层：认领批次、确认映射、口径、定版、差异与追溯。

设计要点：

* 观测表只追加。电表更正通过新的 ``reading/version`` 记录表达，因子追溯通过
  更高的 ``revision`` 表达；历史行永不覆盖。
* 映射和产量口径按 ``[valid_from, valid_to]`` 生效，修改必须显式 supersede，
  避免旧确认悄悄改变已发布月份的解释。
* 发布是快照：保存当时的计算依据、来源观测 ID、因子修订和哈希，后续重算只会
  生成差异，不会改写已发布快照。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from . import metrics
from .errors import ConflictError, IngestFailure, NotReadyError, ValidationError
from .sources import Source, build_source
from .storage import Storage
from .util import (
    CONFIRMATION_STATUSES,
    MAPPING_STATUSES,
    canonical_json,
    day_before,
    normalize_scope,
    parse_number,
    parse_revision,
    payload_hash,
    period_end,
    period_start,
    text_or_none,
    utc_now,
    validate_date,
    validate_period,
)

SOURCE_KINDS = ("meters", "production", "factors")


class Ledger:
    def __init__(self, path: str | Path, *, actor: str = "analyst"):
        self.storage = Storage(path)
        self.storage.initialize()
        self.actor = actor

    def close(self) -> None:
        self.storage.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 采集
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_source(source: str) -> str:
        if source not in SOURCE_KINDS:
            raise ValidationError(f"来源类型必须是 {', '.join(SOURCE_KINDS)} 之一，收到 {source!r}")
        return source

    def start_batch(self, source: str, origin: str, config: dict[str, Any] | None = None,
                    note: str | None = None) -> int:
        source = self._normalize_source(source)
        if not str(origin).strip():
            raise ValidationError("批次必须记录来源标识 origin")
        now = utc_now()
        with self.storage.transaction():
            cursor = self.storage.execute(
                "INSERT INTO batches (source, origin, status, next_page, config_json, "
                "created_at, updated_at, note) VALUES (?, ?, 'open', 1, ?, ?, ?, ?)",
                (source, str(origin), canonical_json(config or {}), now, now, note),
            )
            batch_id = int(cursor.lastrowid)
            self.storage.audit(
                at=now, actor=self.actor, action="batch.start", entity="batch",
                entity_id=batch_id, detail=canonical_json({"source": source, "origin": origin}),
            )
        return batch_id

    def fetch(
        self,
        source: str,
        origin: str,
        *,
        config: dict[str, Any] | None = None,
        page_size: int | None = None,
        note: str | None = None,
        max_pages: int | None = None,
        batch_id: int | None = None,
    ) -> dict[str, Any]:
        """创建或续传一个批次，返回批次摘要。"""
        source = self._normalize_source(source)
        config = dict(config or {})
        if page_size is not None:
            config["page_size"] = int(page_size)
        if batch_id is None:
            batch_id = self.start_batch(source, origin, config, note)
        return self.run_batch(batch_id, max_pages=max_pages)

    def _batch(self, batch_id: int) -> dict[str, Any]:
        row = self.storage.one("SELECT * FROM batches WHERE id = ?", (batch_id,))
        if row is None:
            raise ValidationError(f"找不到批次 {batch_id}")
        return dict(row)

    def _source_for_batch(self, batch: dict[str, Any]) -> Source:
        config = json.loads(batch["config_json"] or "{}")
        config = dict(config)
        config.setdefault("kind", "csv")
        return build_source(config)

    def run_batch(self, batch_id: int, *, max_pages: int | None = None) -> dict[str, Any]:
        batch = self._batch(batch_id)
        if batch["status"] == "complete":
            return self.batch_summary(batch_id)
        source = self._source_for_batch(batch)
        next_page = int(batch["next_page"])
        cursor = batch["cursor"]
        stored = 0
        total_pages = batch["total_pages"]
        try:
            while True:
                if max_pages is not None and stored >= int(max_pages):
                    with self.storage.transaction():
                        self.storage.execute(
                            "UPDATE batches SET updated_at = ? WHERE id = ?", (utc_now(), batch_id)
                        )
                    return {
                        "batch_id": batch_id,
                        "status": "open",
                        "resume_page": next_page,
                        "resume_cursor": cursor,
                        "processed_pages": stored,
                        "total_pages": total_pages,
                    }
                existing = self.storage.one(
                    "SELECT payload_hash FROM batch_pages WHERE batch_id = ? AND page = ?",
                    (batch_id, next_page),
                )
                if existing is not None:
                    # 页与批次进度在同一事务提交，正常不会出现；若出现则校验内容未漂移。
                    page = source.fetch(next_page, cursor)
                    if payload_hash(page.records) != existing["payload_hash"]:
                        raise ConflictError(
                            f"批次 {batch_id} 第 {next_page} 页的来源内容在续传时发生变化；"
                            "为保留可核对性，请新建批次重抓"
                        )
                    if page.next_page is None:
                        self._finish_batch(batch_id, page.total_pages or total_pages)
                        return self.batch_summary(batch_id)
                    next_page = page.next_page
                    cursor = page.next_cursor
                    continue
                page = source.fetch(next_page, cursor)
                page_hash = payload_hash(page.records)
                with self.storage.transaction():
                    self._store_records(batch["source"], batch_id, page.records)
                    self.storage.execute(
                        "INSERT INTO batch_pages (batch_id, page, fetched_at, record_count, payload_hash) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (batch_id, next_page, utc_now(), len(page.records), page_hash),
                    )
                    if page.next_page is None:
                        self.storage.execute(
                            "UPDATE batches SET status = 'complete', next_page = ?, total_pages = ?, "
                            "cursor = NULL, updated_at = ?, completed_at = ?, last_error = NULL WHERE id = ?",
                            (
                                page.page,
                                page.total_pages or total_pages or page.page,
                                utc_now(),
                                utc_now(),
                                batch_id,
                            ),
                        )
                        self.storage.audit(
                            at=utc_now(), actor=self.actor, action="batch.complete",
                            entity="batch", entity_id=batch_id,
                            detail=canonical_json({"pages": page.page, "records": len(page.records)}),
                        )
                        return self.batch_summary(batch_id)
                    self.storage.execute(
                        "UPDATE batches SET next_page = ?, cursor = ?, total_pages = ?, "
                        "updated_at = ?, last_error = NULL WHERE id = ?",
                        (
                            page.next_page,
                            page.next_cursor,
                            page.total_pages or total_pages,
                            utc_now(),
                            batch_id,
                        ),
                    )
                next_page = page.next_page
                cursor = page.next_cursor
                total_pages = page.total_pages or total_pages
                stored += 1
        except (IngestFailure, ConflictError):
            raise
        except Exception as exc:
            now = utc_now()
            with self.storage.transaction():
                self.storage.execute(
                    "UPDATE batches SET status = 'interrupted', next_page = ?, cursor = ?, "
                    "updated_at = ?, last_error = ? WHERE id = ?",
                    (next_page, cursor, now, f"{type(exc).__name__}: {exc}", batch_id),
                )
                self.storage.audit(
                    at=now, actor=self.actor, action="batch.interrupt", entity="batch",
                    entity_id=batch_id, detail=str(exc),
                )
            if isinstance(exc, ValidationError):
                # 来源内容不合法时重试同一页不会成功，按数据错误直接报告。
                raise
            raise IngestFailure(f"批次 {batch_id} 在第 {next_page} 页中断，可续传：{exc}", batch_id) from exc

    def _finish_batch(self, batch_id: int, total_pages: int | None) -> None:
        now = utc_now()
        with self.storage.transaction():
            self.storage.execute(
                "UPDATE batches SET status = 'complete', next_page = ?, total_pages = ?, "
                "updated_at = ?, completed_at = ?, last_error = NULL WHERE id = ?",
                (total_pages or 1, total_pages, now, now, batch_id),
            )
            self.storage.audit(
                at=now, actor=self.actor, action="batch.complete", entity="batch",
                entity_id=batch_id, detail=None,
            )

    def _store_records(self, source: str, batch_id: int, records: Iterable[dict[str, Any]]) -> None:
        for raw in records:
            raw = dict(raw)
            if source == "meters":
                self._store_meter(batch_id, raw)
            elif source == "production":
                self._store_production(batch_id, raw)
            else:
                self._store_factor(batch_id, raw)

    @staticmethod
    def _require(raw: dict[str, Any], field: str, label: str) -> str:
        value = text_or_none(raw.get(field))
        if value is None:
            raise ValidationError(f"{label} 缺少字段 {field}")
        return value

    def _store_meter(self, batch_id: int, raw: dict[str, Any]) -> None:
        meter = self._require(raw, "meter", "电表观测")
        period = validate_period(raw.get("period"), "电表观测 period")
        version = self._require(raw, "version", "电表观测")
        identity = text_or_none(raw.get("reading"))
        if identity is None:
            # 来源没有独立读数号时，用计量点+期间+版本构成唯一键，仍保留原始行。
            identity = f"{meter}:{period}:{version}"
        reading = identity
        line = self._require(raw, "line", "电表观测")
        kwh = parse_number(raw.get("kwh"), f"电表 {meter} 在 {period} 的 kwh", required=False)
        digest = payload_hash(raw)
        existing = self.storage.one(
            "SELECT * FROM meter_observations WHERE meter = ? AND reading = ? AND version = ?",
            (meter, reading, version),
        )
        if existing is not None:
            if existing["payload_hash"] != digest:
                raise ConflictError(
                    f"电表观测 {meter}/{reading}/{version} 已存在且内容不同；更正请使用新的 version"
                )
            return
        self.storage.execute(
            "INSERT INTO meter_observations (batch_id, reading, meter, line, period, kwh, "
            "version, raw_json, payload_hash, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, reading, meter, line, period, kwh, version,
             canonical_json(raw), digest, utc_now()),
        )

    def _store_production(self, batch_id: int, raw: dict[str, Any]) -> None:
        row_id = self._require(raw, "row_id", "产量观测")
        line = self._require(raw, "line", "产量观测")
        period = validate_period(raw.get("period"), "产量观测 period")
        good_units = parse_number(raw.get("good_units"), f"产量 {row_id} 的 good_units", required=False)
        scope_raw = text_or_none(raw.get("scope"))
        scope = normalize_scope(scope_raw)
        # 来源若提供版本就用它；不提供则用空串，由入库顺序表示修订先后。
        version = text_or_none(raw.get("version")) or text_or_none(raw.get("revision")) or ""
        digest = payload_hash(raw)
        existing = self.storage.all(
            "SELECT * FROM production_observations WHERE row_id = ? AND version = ?",
            (row_id, version),
        )
        if existing:
            if any(row["payload_hash"] == digest for row in existing):
                return
            if version:
                raise ConflictError(
                    f"产量行 {row_id} 的版本 {version} 已存在且内容不同；修订请提供新的 version"
                )
            # 未提供版本：来源以同号行补传更正，追加为新观测，按入库顺序取最新。
        self.storage.execute(
            "INSERT INTO production_observations (batch_id, row_id, line, period, good_units, "
            "scope, scope_raw, version, raw_json, payload_hash, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, row_id, line, period, good_units, scope, scope_raw, version,
             canonical_json(raw), digest, utc_now()),
        )

    def _store_factor(self, batch_id: int, raw: dict[str, Any]) -> None:
        factor = self._require(raw, "factor", "因子观测")
        source = self._require(raw, "source", "因子观测")
        factor_key = text_or_none(raw.get("factor_key")) or ""
        period = validate_period(raw.get("period"), "因子观测 period")
        revision = parse_revision(raw.get("revision"))
        value = parse_number(raw.get("value"), f"因子 {source}/{factor} 在 {period} 的 value", required=False)
        effective_from = raw.get("effective_from")
        effective_to = raw.get("effective_to")
        if text_or_none(effective_from) is None:
            effective_from = period_start(period)
        else:
            effective_from = validate_date(effective_from, "effective_from")
        if text_or_none(effective_to) is None:
            effective_to = period_end(period)
        else:
            effective_to = validate_date(effective_to, "effective_to")
        if effective_from > effective_to:
            raise ValidationError(
                f"因子 {source}/{factor} r{revision} 的生效区间为空：{effective_from}..{effective_to}"
            )
        digest = payload_hash(raw)
        existing = self.storage.one(
            "SELECT * FROM factor_observations WHERE source = ? AND factor_key = ? "
            "AND period = ? AND revision = ?",
            (source, factor_key, period, revision),
        )
        if existing is not None:
            if existing["payload_hash"] != digest:
                raise ConflictError(
                    f"因子来源 {source} 的系列 {factor_key or '(默认)'} 在 {period} 的修订 "
                    f"r{revision} 已存在且内容不同；追溯修订请递增 revision，"
                    "同一来源有多个因子时请提供 factor_key 区分系列"
                )
            return
        self.storage.execute(
            "INSERT INTO factor_observations (batch_id, factor, source, factor_key, period, value, "
            "revision, effective_from, effective_to, raw_json, payload_hash, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, factor, source, factor_key, period, value, revision, effective_from,
             effective_to, canonical_json(raw), digest, utc_now()),
        )

    def batch_summary(self, batch_id: int) -> dict[str, Any]:
        batch = self._batch(batch_id)
        pages = self.storage.all(
            "SELECT * FROM batch_pages WHERE batch_id = ? ORDER BY page", (batch_id,)
        )
        counts = {
            "meters": "meter_observations",
            "production": "production_observations",
            "factors": "factor_observations",
        }
        records = self.storage.one(
            f"SELECT COUNT(*) AS n FROM {counts[batch['source']]} WHERE batch_id = ?", (batch_id,)
        )
        return {
            "batch_id": batch_id,
            "source": batch["source"],
            "origin": batch["origin"],
            "status": batch["status"],
            "next_page": batch["next_page"],
            "total_pages": batch["total_pages"],
            "pages_stored": len(pages),
            "records_stored": records["n"] if records else 0,
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
            "completed_at": batch["completed_at"],
            "last_error": batch["last_error"],
            "note": batch["note"],
        }

    def list_batches(self) -> list[dict[str, Any]]:
        return [
            self.batch_summary(row["id"])
            for row in self.storage.all("SELECT id FROM batches ORDER BY id")
        ]

    # ------------------------------------------------------------------
    # 人工确认
    # ------------------------------------------------------------------
    def confirm_meter_mapping(
        self,
        meter: str,
        line: str,
        valid_from: str,
        valid_to: str,
        status: str = "confirmed",
        *,
        replaces_meter: str | None = None,
        supersedes_id: int | None = None,
        note: str | None = None,
        decided_by: str | None = None,
    ) -> int:
        """登记计量点到产线的有效期关系（确认/部分对应/待核）。

        同一计量点的确认区间不得重叠；纠正历史确认时传入 ``supersedes_id``，
        旧记录会在新记录起始日前一天截止，并保留完整审计。
        """
        meter = str(meter).strip()
        line = str(line).strip()
        if not meter or not line:
            raise ValidationError("meter 和 line 不能为空")
        if status not in MAPPING_STATUSES:
            raise ValidationError(f"映射状态必须是 {', '.join(MAPPING_STATUSES)} 之一")
        valid_from = validate_date(valid_from, "valid_from")
        valid_to = validate_date(valid_to, "valid_to")
        if valid_from > valid_to:
            raise ValidationError("valid_from 不能晚于 valid_to")
        replaces_meter = text_or_none(replaces_meter)
        if replaces_meter == meter:
            raise ValidationError("replaces_meter 不能是计量点自身")
        actor = decided_by or self.actor
        now = utc_now()
        superseded: dict[str, Any] | None = None
        with self.storage.transaction():
            if supersedes_id is not None:
                old = self.storage.one("SELECT * FROM meter_mappings WHERE id = ?", (supersedes_id,))
                if old is None:
                    raise ValidationError(f"找不到要取代的映射 {supersedes_id}")
                if old["meter"] != meter:
                    raise ValidationError("只能取代同一计量点的映射")
                if valid_from <= old["valid_from"]:
                    raise ValidationError("替代映射的起始日期必须晚于被替代映射")
                if valid_from <= old["valid_to"]:
                    truncated = day_before(valid_from)
                    if truncated < old["valid_from"]:
                        raise ValidationError("替代映射会让被替代映射的生效区间变空")
                    superseded = {
                        "id": old["id"],
                        "previous_valid_to": old["valid_to"],
                        "new_valid_to": truncated,
                    }
                    self.storage.execute(
                        "UPDATE meter_mappings SET valid_to = ? WHERE id = ?",
                        (truncated, supersedes_id),
                    )
            overlaps = self.storage.all(
                "SELECT * FROM meter_mappings WHERE meter = ? AND valid_from <= ? AND valid_to >= ? "
                "ORDER BY valid_from",
                (meter, valid_to, valid_from),
            )
            if overlaps:
                ids = ", ".join(str(row["id"]) for row in overlaps)
                raise ConflictError(
                    f"计量点 {meter} 在 {valid_from}..{valid_to} 已有生效映射 {ids}；"
                    "请先确认是否要 supersede 或用更窄的区间"
                )
            cursor = self.storage.execute(
                "INSERT INTO meter_mappings (meter, line, valid_from, valid_to, status, "
                "replaces_meter, supersedes_id, decided_by, decided_at, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (meter, line, valid_from, valid_to, status, replaces_meter, supersedes_id,
                 actor, now, note, now),
            )
            mapping_id = int(cursor.lastrowid)
            self.storage.audit(
                at=now, actor=actor, action="mapping.confirm", entity="meter_mapping",
                entity_id=mapping_id,
                detail=canonical_json(
                    {"meter": meter, "line": line, "valid_from": valid_from,
                     "valid_to": valid_to, "status": status,
                     "replaces_meter": replaces_meter,
                     "supersedes_id": supersedes_id,
                     "superseded": None if supersedes_id is None else superseded}
                ),
            )
        return mapping_id

    def retract_meter_mapping(self, mapping_id: int, *, reason: str | None = None,
                              decided_by: str | None = None) -> None:
        """把映射降级为待核，而不是删除，保留历史。"""
        actor = decided_by or self.actor
        now = utc_now()
        with self.storage.transaction():
            row = self.storage.one("SELECT * FROM meter_mappings WHERE id = ?", (mapping_id,))
            if row is None:
                raise ValidationError(f"找不到映射 {mapping_id}")
            self.storage.execute(
                "UPDATE meter_mappings SET status = 'pending', decided_by = ?, decided_at = ?, "
                "note = COALESCE(?, note) WHERE id = ?",
                (actor, now, reason, mapping_id),
            )
            self.storage.audit(
                at=now, actor=actor, action="mapping.retract", entity="meter_mapping",
                entity_id=mapping_id, detail=reason,
            )

    def list_meter_mappings(self, meter: str | None = None) -> list[dict[str, Any]]:
        if meter:
            rows = self.storage.all(
                "SELECT * FROM meter_mappings WHERE meter = ? ORDER BY valid_from, id", (meter,)
            )
        else:
            rows = self.storage.all("SELECT * FROM meter_mappings ORDER BY meter, valid_from, id")
        return [dict(row) for row in rows]

    def confirm_production_scope(
        self,
        line: str,
        valid_from: str,
        valid_to: str,
        *,
        scope: str = "final",
        product: str | None = None,
        process_version: str | None = None,
        status: str = "confirmed",
        supersedes_id: int | None = None,
        note: str | None = None,
        decided_by: str | None = None,
    ) -> int:
        """生产人员确认产线的产品、工艺版本和合格产量统计范围。"""
        line = str(line).strip()
        if not line:
            raise ValidationError("line 不能为空")
        if status not in CONFIRMATION_STATUSES:
            raise ValidationError(f"确认状态必须是 {', '.join(CONFIRMATION_STATUSES)} 之一")
        scope = normalize_scope(scope) or ""
        if scope.startswith("unknown"):
            raise ValidationError("scope 必须是 final 或 includes-rework")
        valid_from = validate_date(valid_from, "valid_from")
        valid_to = validate_date(valid_to, "valid_to")
        if valid_from > valid_to:
            raise ValidationError("valid_from 不能晚于 valid_to")
        actor = decided_by or self.actor
        now = utc_now()
        product = text_or_none(product)
        process_version = text_or_none(process_version)
        superseded: dict[str, Any] | None = None
        with self.storage.transaction():
            if supersedes_id is not None:
                old = self.storage.one("SELECT * FROM production_scopes WHERE id = ?", (supersedes_id,))
                if old is None:
                    raise ValidationError(f"找不到要取代的口径确认 {supersedes_id}")
                if old["line"] != line:
                    raise ValidationError("只能取代同一产线的口径确认")
                if valid_from <= old["valid_from"]:
                    raise ValidationError("替代确认的起始日期必须晚于被替代确认")
                if valid_from <= old["valid_to"]:
                    truncated = day_before(valid_from)
                    if truncated < old["valid_from"]:
                        raise ValidationError("替代确认会让被替代确认的生效区间变空")
                    superseded = {
                        "id": old["id"],
                        "previous_valid_to": old["valid_to"],
                        "new_valid_to": truncated,
                    }
                    self.storage.execute(
                        "UPDATE production_scopes SET valid_to = ? WHERE id = ?",
                        (truncated, supersedes_id),
                    )
            overlaps = self.storage.all(
                "SELECT * FROM production_scopes WHERE line = ? AND valid_from <= ? AND valid_to >= ?",
                (line, valid_to, valid_from),
            )
            if overlaps:
                ids = ", ".join(str(row["id"]) for row in overlaps)
                raise ConflictError(
                    f"产线 {line} 在 {valid_from}..{valid_to} 已有口径确认 {ids}；"
                    "请先确认是否要 supersede"
                )
            cursor = self.storage.execute(
                "INSERT INTO production_scopes (line, valid_from, valid_to, status, scope, "
                "product, process_version, supersedes_id, decided_by, decided_at, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (line, valid_from, valid_to, status, scope, product, process_version,
                 supersedes_id, actor, now, note, now),
            )
            scope_id = int(cursor.lastrowid)
            self.storage.audit(
                at=now, actor=actor, action="scope.confirm", entity="production_scope",
                entity_id=scope_id,
                detail=canonical_json(
                    {"line": line, "valid_from": valid_from, "valid_to": valid_to,
                     "scope": scope, "product": product, "process_version": process_version,
                     "status": status, "supersedes_id": supersedes_id, "superseded": superseded}
                ),
            )
        return scope_id

    def list_production_scopes(self, line: str | None = None) -> list[dict[str, Any]]:
        if line:
            rows = self.storage.all(
                "SELECT * FROM production_scopes WHERE line = ? ORDER BY valid_from, id", (line,)
            )
        else:
            rows = self.storage.all("SELECT * FROM production_scopes ORDER BY line, valid_from, id")
        return [dict(row) for row in rows]

    def set_factor_selection(self, source: str, factor_key: str = "", *, period: str | None = None) -> None:
        """指定园区适用的公开因子系列；存在多来源时必须显式选择。

        系列由 ``source`` 与 ``factor_key`` 共同标识；传入 ``period`` 时只约束该月，
        否则作为所有月份的默认选择。
        """
        source = str(source).strip()
        factor_key = str(factor_key or "").strip()
        if not source:
            raise ValidationError("source 不能为空")
        key = "factor_selection"
        if period is not None:
            key = f"factor_selection:{validate_period(period)}"
        now = utc_now()
        with self.storage.transaction():
            self.storage.set_setting(key, canonical_json({"source": source, "factor_key": factor_key}))
            self.storage.audit(
                at=now, actor=self.actor, action="factor.select", entity="setting",
                entity_id=key,
                detail=canonical_json({"source": source, "factor_key": factor_key, "period": period}),
            )

    # ------------------------------------------------------------------
    # 计算、定版
    # ------------------------------------------------------------------
    def compute(self, period: str) -> dict[str, Any]:
        return metrics.compute_period(self.storage, validate_period(period))

    def publish(self, period: str, *, note: str | None = None, allow_partial: bool = False,
                published_by: str | None = None) -> dict[str, Any]:
        period = validate_period(period)
        draft = self.compute(period)
        if draft["factor_selection"] is None:
            reasons = [item["message"] for item in _factor_issues(draft)]
            raise NotReadyError("因子口径未确定，不能定版：" + "；".join(reasons or ["没有可用因子"]))
        if not draft["comparable_lines"]:
            raise NotReadyError(
                f"{period} 没有任何可比较产线，不能定版："
                + "；".join(
                    f"{item['line']}: {item['reasons'][0]}" for item in draft["incomparable"]
                )
            )
        if draft["incomparable"] and not allow_partial:
            raise NotReadyError(
                f"{period} 仍有 {len(draft['incomparable'])} 条产线达不到可发布条件；"
                "需确认后重试，或显式允许部分发布并保留不可比原因："
                + "；".join(f"{item['line']}" for item in draft["incomparable"])
            )
        actor = published_by or self.actor
        now = utc_now()
        with self.storage.transaction():
            previous = self.storage.one(
                "SELECT * FROM releases WHERE period = ? ORDER BY revision DESC LIMIT 1",
                (period,),
            )
            revision = 1 if previous is None else int(previous["revision"]) + 1
            payload = {
                "period": period,
                "revision": revision,
                "published_by": actor,
                "published_at": now,
                "note": note,
                "allow_partial": bool(allow_partial),
                "draft": draft,
                "inputs": self._input_manifest(period),
            }
            digest = payload_hash(payload)
            cursor = self.storage.execute(
                "INSERT INTO releases (period, revision, published_by, published_at, note, "
                "payload_json, payload_hash, supersedes_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (period, revision, actor, now, note, canonical_json(payload), digest,
                 previous["id"] if previous else None),
            )
            release_id = int(cursor.lastrowid)
            self.storage.audit(
                at=now, actor=actor, action="release.publish", entity="release",
                entity_id=release_id,
                detail=canonical_json(
                    {"period": period, "revision": revision, "hash": digest,
                     "comparable": draft["comparable_lines"],
                     "incomparable": [item["line"] for item in draft["incomparable"]]}
                ),
            )
        return self.get_release_by_id(release_id)

    def _input_manifest(self, period: str) -> dict[str, Any]:
        """冻结当时使用的观测、映射、口径和批次 ID，供事后核对。"""
        tables = {
            "meter_observations": "SELECT id, batch_id, reading, meter, line, period, kwh, version, "
                                  "fetched_at FROM meter_observations WHERE period = ? ORDER BY id",
            "production_observations": "SELECT id, batch_id, row_id, line, period, good_units, scope, "
                                       "scope_raw, fetched_at FROM production_observations "
                                       "WHERE period = ? ORDER BY id",
            "factor_observations": "SELECT id, batch_id, factor, source, factor_key, period, value, "
                                   "revision, effective_from, effective_to, fetched_at "
                                   "FROM factor_observations WHERE effective_from <= ? "
                                   "AND effective_to >= ? ORDER BY id",
        }
        manifest: dict[str, list[dict[str, Any]]] = {}
        for name, sql in tables.items():
            if name == "factor_observations":
                params = (period_end(period), period_start(period))
            else:
                params = (period,)
            manifest[name] = [dict(row) for row in self.storage.all(sql, params)]
        manifest["meter_mappings"] = [
            dict(row) for row in self.storage.all(
                "SELECT * FROM meter_mappings WHERE valid_from <= ? AND valid_to >= ? ORDER BY id",
                (period_end(period), period_start(period)),
            )
        ]
        manifest["production_scopes"] = [
            dict(row) for row in self.storage.all(
                "SELECT * FROM production_scopes WHERE valid_from <= ? AND valid_to >= ? ORDER BY id",
                (period_end(period), period_start(period)),
            )
        ]
        manifest["batches"] = [
            dict(row) for row in self.storage.all(
                "SELECT id, source, origin, status, completed_at FROM batches "
                "WHERE id IN (SELECT batch_id FROM meter_observations WHERE period = ? "
                "UNION SELECT batch_id FROM production_observations WHERE period = ? "
                "UNION SELECT batch_id FROM factor_observations "
                "WHERE effective_from <= ? AND effective_to >= ?) ORDER BY id",
                (period, period, period_end(period), period_start(period)),
            )
        ]
        manifest["factor_selection"] = metrics.resolve_factor_selection(self.storage, period)[0]
        return manifest

    def get_release_by_id(self, release_id: int) -> dict[str, Any]:
        row = self.storage.one("SELECT * FROM releases WHERE id = ?", (release_id,))
        if row is None:
            raise ValidationError(f"找不到发布记录 {release_id}")
        item = dict(row)
        payload = json.loads(item["payload_json"])
        expected = payload_hash(payload)
        if expected != item["payload_hash"]:
            raise ConflictError(
                f"发布记录 {release_id} 的内容哈希与保存值不一致，快照可能被改动"
            )
        item["payload"] = payload
        return item

    def get_release(self, period: str, revision: int | None = None) -> dict[str, Any]:
        period = validate_period(period)
        if revision is None:
            row = self.storage.one(
                "SELECT id FROM releases WHERE period = ? ORDER BY revision DESC LIMIT 1", (period,)
            )
        else:
            row = self.storage.one(
                "SELECT id FROM releases WHERE period = ? AND revision = ?", (period, int(revision))
            )
        if row is None:
            suffix = f" r{revision}" if revision is not None else ""
            raise ValidationError(f"找不到 {period}{suffix} 的发布记录")
        return self.get_release_by_id(row["id"])

    def list_releases(self, period: str | None = None) -> list[dict[str, Any]]:
        if period:
            rows = self.storage.all(
                "SELECT id FROM releases WHERE period = ? ORDER BY revision DESC", (validate_period(period),)
            )
        else:
            rows = self.storage.all("SELECT id FROM releases ORDER BY period, revision DESC")
        return [self.get_release_by_id(row["id"]) for row in rows]

    # ------------------------------------------------------------------
    # 差异、影响分析与追溯
    # ------------------------------------------------------------------
    def impact(self, period: str) -> dict[str, Any]:
        """比较最新发布与当前重算，指出排名、汇总和需重新签发的范围。"""
        period = validate_period(period)
        try:
            release = self.get_release(period)
        except ValidationError:
            release = None
        current = self.compute(period)
        if release is None:
            return {
                "period": period,
                "release": None,
                "current": current,
                "changed": True,
                "needs_reissue": False,
                "message": "该月尚未发布，本次为首次定版前的重算",
                "line_changes": [],
                "park_summary": {"before": None, "after": current["park_summary"]},
                "ranking_changes": [],
                "factor_revision_changes": [],
                "release_changes": {},
            }
        previous = release["payload"]["draft"]
        line_changes = _compare_lines(previous, current)
        ranking_changes = _compare_ranking(previous, current)
        factor_before = previous.get("factor_selection")
        factor_after = current.get("factor_selection")
        applied_before = _applied_factors(previous)
        applied_after = _applied_factors(current)
        factor_revisions = _compare_applied_factors(applied_before, applied_after)
        input_changes = self._new_inputs_since(release)
        changed = bool(line_changes or ranking_changes or factor_before != factor_after
                       or factor_revisions or _park_differs(previous["park_summary"], current["park_summary"]))
        affected_lines = {item["line"] for item in line_changes}
        for item in ranking_changes:
            affected_lines.update(item.get("lines", []))
        affected_lines.update(item["line"] for item in factor_revisions)
        needs_reissue = changed
        return {
            "period": period,
            "release": {
                "id": release["id"],
                "period": release["period"],
                "revision": release["revision"],
                "published_at": release["published_at"],
                "published_by": release["published_by"],
                "payload_hash": release["payload_hash"],
            },
            "current": current,
            "changed": changed,
            "needs_reissue": needs_reissue,
            "reissue_scope": {
                "lines": sorted(affected_lines),
                "factor_changed": factor_before != factor_after or bool(factor_revisions),
                "factor_before": factor_before,
                "factor_after": factor_after,
            },
            "line_changes": line_changes,
            "park_summary": {"before": previous["park_summary"], "after": current["park_summary"]},
            "ranking_changes": ranking_changes,
            "factor_revision_changes": factor_revisions,
            "release_changes": input_changes,
            "message": (
                "已发布快照保持不变；当前可计算数据与发布时不同，可按需重新签发"
                if changed else "当前数据与已发布快照一致"
            ),
        }

    def _new_inputs_since(self, release: dict[str, Any]) -> dict[str, Any]:
        """以发布快照的记录 ID 为准，列出之后新增的观测和确认。

        不依赖时间戳比较，避免同一秒内重算或导入造成的漏判。
        """
        manifest = release["payload"].get("inputs", {})
        result: dict[str, Any] = {}
        tables = {
            "meter_observations": "meter_observations",
            "production_observations": "production_observations",
            "factor_observations": "factor_observations",
        }
        for name, table in tables.items():
            known = {int(row["id"]) for row in manifest.get(name, [])}
            if name == "factor_observations":
                rows = self.storage.all(
                    f"SELECT id, batch_id, period FROM {table} "
                    "WHERE effective_from <= ? AND effective_to >= ? ORDER BY id",
                    (period_end(release["period"]), period_start(release["period"])),
                )
            else:
                rows = self.storage.all(
                    f"SELECT id, batch_id, period FROM {table} WHERE period = ? ORDER BY id",
                    (release["period"],),
                )
            result[name] = [
                dict(row) for row in rows if int(row["id"]) not in known
            ]
        known_mappings = {int(row["id"]) for row in manifest.get("meter_mappings", [])}
        result["meter_mappings"] = [
            dict(row) for row in self.storage.all(
                "SELECT id, meter, line, status, decided_at FROM meter_mappings "
                "WHERE valid_from <= ? AND valid_to >= ? ORDER BY id",
                (period_end(release["period"]), period_start(release["period"])),
            )
            if int(row["id"]) not in known_mappings
        ]
        known_scopes = {int(row["id"]) for row in manifest.get("production_scopes", [])}
        result["production_scopes"] = [
            dict(row) for row in self.storage.all(
                "SELECT id, line, scope, status, decided_at FROM production_scopes "
                "WHERE valid_from <= ? AND valid_to >= ? ORDER BY id",
                (period_end(release["period"]), period_start(release["period"])),
            )
            if int(row["id"]) not in known_scopes
        ]
        return result

    def trace(self, period: str, line: str) -> dict[str, Any]:
        """返回单条指标的计算口径和全部原始观测（含历史版本）。"""
        period = validate_period(period)
        draft = self.compute(period)
        item = next((entry for entry in draft["lines"] if entry["line"] == line), None)
        if item is None:
            raise ValidationError(f"{period} 没有产线 {line} 的记录")
        meters = [row["meter"] for row in item["evidence"]["meters"]]
        observations = {}
        for meter in meters:
            observations[meter] = [
                dict(row) for row in self.storage.all(
                    "SELECT * FROM meter_observations WHERE meter = ? AND period = ? ORDER BY id",
                    (meter, period),
                )
            ]
        production = [
            dict(row) for row in self.storage.all(
                "SELECT * FROM production_observations WHERE line = ? AND period = ? ORDER BY id",
                (line, period),
            )
        ]
        factors = item["evidence"]["factor"] or {}
        factor_rows = []
        if factors.get("selection"):
            selection = factors["selection"]
            factor_rows = [
                dict(row) for row in self.storage.all(
                    "SELECT * FROM factor_observations WHERE source = ? AND factor_key = ? "
                    "ORDER BY effective_from, revision, id",
                    (selection["source"], selection["factor_key"]),
                )
            ]
        return {
            "period": period,
            "line": line,
            "metric": item,
            "raw": {
                "meter_observations": observations,
                "production_observations": production,
                "factor_observations": factor_rows,
            },
        }

    def compare_periods(self, line: str, period_a: str, period_b: str) -> dict[str, Any]:
        """同一条产线两个月的单位能耗比较；任一月不可发布则说明原因。"""
        period_a = validate_period(period_a)
        period_b = validate_period(period_b)
        left = self.compute(period_a)
        right = self.compute(period_b)
        left_line = next((x for x in left["lines"] if x["line"] == line), None)
        right_line = next((x for x in right["lines"] if x["line"] == line), None)
        if left_line is None or right_line is None:
            missing = [p for p, item in ((period_a, left_line), (period_b, right_line)) if item is None]
            raise ValidationError(f"产线 {line} 在 {', '.join(missing)} 没有记录")
        comparable = left_line["status"] == "ready" and right_line["status"] == "ready"
        result: dict[str, Any] = {
            "line": line,
            "period_a": period_a,
            "period_b": period_b,
            "comparable": comparable,
            "a": _metric_brief(left_line),
            "b": _metric_brief(right_line),
            "reasons": [] if comparable else [
                {"period": item["period"], "reasons": [issue["message"] for issue in item["issues"]]}
                for item in (left_line, right_line) if item["status"] != "ready"
            ],
        }
        if comparable:
            a = left_line["metric"]["converted_per_unit"]
            b = right_line["metric"]["converted_per_unit"]
            result["change"] = {
                "absolute": b - a,
                "relative": (b - a) / a if a else None,
            }
        return result

    def audit_log(self, limit: int = 100) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.storage.all(
                "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (int(limit),)
            )
        ]

    def list_observations(self, period: str, line: str | None = None) -> dict[str, Any]:
        period = validate_period(period)
        clause = " AND line = ?" if line else ""
        params: tuple = (period, line) if line else (period,)
        return {
            "period": period,
            "meter_observations": [
                dict(row) for row in self.storage.all(
                    "SELECT * FROM meter_observations WHERE period = ?" + clause + " ORDER BY id",
                    params,
                )
            ],
            "production_observations": [
                dict(row) for row in self.storage.all(
                    "SELECT * FROM production_observations WHERE period = ?" + clause + " ORDER BY id",
                    params,
                )
            ],
            "factor_observations": [
                dict(row) for row in self.storage.all(
                    "SELECT * FROM factor_observations WHERE period = ? ORDER BY id", (period,)
                )
            ],
        }

    def export_release(self, period: str, revision: int | None = None) -> dict[str, Any]:
        release = self.get_release(period, revision)
        return {
            "release": {
                "id": release["id"],
                "period": release["period"],
                "revision": release["revision"],
                "published_at": release["published_at"],
                "published_by": release["published_by"],
                "note": release["note"],
                "payload_hash": release["payload_hash"],
            },
            "draft": release["payload"]["draft"],
            "inputs": release["payload"]["inputs"],
        }


def _applied_factors(draft: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """从草稿中提取已生成指标的产线实际采用的因子修订，供影响分析归因。

    只有可比较产线才有发布值；不可比产线即使算过因子，也不属于重新签发范围。
    """
    result: dict[str, dict[str, Any]] = {}
    for line in draft["lines"]:
        if line["status"] != "ready" or line["metric"] is None:
            continue
        evidence = line["evidence"].get("factor") or {}
        applied = evidence.get("applied")
        if applied:
            result[line["line"]] = {
                "id": applied.get("id"),
                "revision": applied.get("revision"),
                "value": applied.get("value"),
            }
    return result


def _compare_applied_factors(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    changes = []
    for line in sorted(set(before) | set(after)):
        left, right = before.get(line), after.get(line)
        if left == right:
            continue
        changes.append(
            {
                "line": line,
                "before": left,
                "after": right,
                "revision_changed": None
                if left is None or right is None
                else left.get("revision") != right.get("revision"),
            }
        )
    return changes


def _factor_issues(draft: dict[str, Any]) -> list[dict[str, Any]]:
    for line in draft["lines"]:
        for issue in line["issues"]:
            if issue["code"].startswith("factor_"):
                return [issue]
    return []


def _metric_brief(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": item["status"],
        "metric": item["metric"],
        "issues": item["issues"],
    }


def _compare_lines(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    previous_by_line = {item["line"]: item for item in previous["lines"]}
    current_by_line = {item["line"]: item for item in current["lines"]}
    changes = []
    for line in sorted(set(previous_by_line) | set(current_by_line)):
        before = previous_by_line.get(line)
        after = current_by_line.get(line)
        before_value = None if before is None or before["metric"] is None else before["metric"]["converted_per_unit"]
        after_value = None if after is None or after["metric"] is None else after["metric"]["converted_per_unit"]
        before_status = None if before is None else before["status"]
        after_status = None if after is None else after["status"]
        if before_status == after_status and before_value == after_value:
            continue
        change = {
            "line": line,
            "before_status": before_status,
            "after_status": after_status,
            "before": before_value,
            "after": after_value,
            "delta": None if before_value is None or after_value is None else after_value - before_value,
            "before_reasons": [] if before is None else [i["message"] for i in before["issues"]],
            "after_reasons": [] if after is None else [i["message"] for i in after["issues"]],
        }
        changes.append(change)
    return changes


def _compare_ranking(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    before = {item["line"]: item["rank"] for item in previous["ranking"]}
    after = {item["line"]: item["rank"] for item in current["ranking"]}
    changes = []
    for line in sorted(set(before) & set(after)):
        if before[line] != after[line]:
            changes.append({"line": line, "before_rank": before[line], "after_rank": after[line]})
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    if added:
        changes.append({"lines": added, "change": "entered_ranking"})
    if removed:
        changes.append({"lines": removed, "change": "left_ranking"})
    return changes


def _park_differs(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = ("total_energy_kwh", "total_good_units", "converted_per_unit", "kwh_per_unit")
    for key in keys:
        left, right = before.get(key), after.get(key)
        if left is None or right is None:
            if left is not right:
                return True
        elif abs(float(left) - float(right)) > 1e-12:
            return True
    return False
