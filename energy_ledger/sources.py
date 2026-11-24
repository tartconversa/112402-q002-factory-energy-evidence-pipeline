"""数据来源适配器。

所有来源都按“页”返回 :class:`Page`。批次的页号和游标持久化在 SQLite 中，
因此网络中断或进程退出后可以从下一页继续，不需要重新抓取已提交页面。
"""
from __future__ import annotations

import csv
import io
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

from .errors import LedgerError, ValidationError

DEFAULT_TIMEOUT = 30

SOURCE_PARAMS: dict[str, set[str]] = {
    "csv": {"path", "page_size", "source_columns", "period_from", "period_to", "line"},
    "http-json": {
        "url", "token_env", "token_prefix", "records_path", "next_path", "total_path",
        "page_param", "cursor_param", "page_size_param", "page_size", "timeout",
        "headers", "query", "period_from", "period_to", "line",
    },
    "json-file": {
        "path", "records_path", "next_path", "total_path", "page_size",
        "period_from", "period_to", "line",
    },
    "html-table": {
        "path", "path_or_url", "url", "table_index", "columns", "token_env",
        "token_prefix", "timeout", "period_from", "period_to", "line",
    },
}

SECRET_KEYS = {
    "token", "password", "secret", "api_key", "apikey", "api-key", "access_token",
    "client_secret", "authorization", "cookie", "x-api-key",
}


def _assert_no_inline_secrets(value: Any, path: str = "config") -> None:
    """拒绝把令牌直接写进配置，避免它随批次落库。

    授权令牌应放在环境变量中，通过 ``token_env`` 指定变量名。
    """
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).lower()
            if lowered in SECRET_KEYS:
                raise ValidationError(
                    f"{path}.{key} 看起来是凭据；请改用 token_env 指向环境变量，避免写入数据库"
                )
            _assert_no_inline_secrets(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_inline_secrets(child, f"{path}[{index}]")


@dataclass(frozen=True)
class Page:
    records: list[dict[str, Any]]
    page: int
    next_page: int | None = None
    next_cursor: str | None = None
    total_pages: int | None = None


class Source(Protocol):
    def describe(self) -> dict[str, Any]:
        ...

    def fetch(self, page: int, cursor: str | None) -> Page:
        ...


def _filtered(rows: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    period_from = config.get("period_from")
    period_to = config.get("period_to")
    line_filter = config.get("line")
    if not period_from and not period_to and not line_filter:
        return rows
    kept = []
    for row in rows:
        period = str(row.get("period", ""))
        if period_from and period < str(period_from):
            continue
        if period_to and period > str(period_to):
            continue
        if line_filter and str(row.get("line", "")) != str(line_filter):
            continue
        kept.append(row)
    return kept


class CsvSource:
    """本地 CSV 导出文件，按 ``page_size`` 切页。

    补传时用 ``period_from``/``period_to`` 限定月份，避免整表重抓。
    """

    def __init__(self, path: str, *, page_size: int = 100, source_columns: dict[str, str] | None = None,
                 **filters: Any):
        if page_size < 1:
            raise ValidationError("page_size 必须为正整数")
        self.path = str(Path(path).expanduser())
        self.page_size = int(page_size)
        self.source_columns = {str(k): str(v) for k, v in (source_columns or {}).items()}
        self.filters = {k: v for k, v in filters.items() if v not in (None, "")}
        self._rows: list[dict[str, Any]] | None = None

    def describe(self) -> dict[str, Any]:
        return {
            "kind": "csv",
            "path": self.path,
            "page_size": self.page_size,
            "source_columns": self.source_columns,
            **self.filters,
        }

    def _load(self) -> list[dict[str, Any]]:
        if self._rows is None:
            file = Path(self.path)
            if not file.is_file():
                raise ValidationError(f"找不到 CSV 文件：{self.path}")
            with file.open(encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source, strict=True)
                if reader.fieldnames is None:
                    raise ValidationError(f"CSV 缺少表头：{self.path}")
                missing = [name for name in self.source_columns if name not in reader.fieldnames]
                if missing:
                    raise ValidationError(f"{self.path} 缺少列：{', '.join(missing)}")
                rows = []
                for number, row in enumerate(reader, start=2):
                    if None in row or None in row.values():
                        raise ValidationError(f"{self.path} 第 {number} 行列数不匹配")
                    item = dict(row)
                    for original, standard in self.source_columns.items():
                        if standard in item and item[standard] not in (None, ""):
                            raise ValidationError(
                                f"{self.path} 第 {number} 行同时有 {original} 和 {standard}，无法判定用哪列"
                            )
                        item[standard] = item.pop(original)
                    rows.append(item)
            self._rows = _filtered(rows, self.filters)
        return self._rows

    def fetch(self, page: int, cursor: str | None) -> Page:
        rows = self._load()
        if page < 1:
            raise ValidationError(f"页号必须从 1 开始，收到 {page}")
        start = (page - 1) * self.page_size
        chunk = rows[start : start + self.page_size]
        total_pages = max(1, (len(rows) + self.page_size - 1) // self.page_size)
        next_page = page + 1 if page < total_pages else None
        return Page(
            records=chunk,
            page=page,
            next_page=next_page,
            next_cursor=None if next_page is None else str(next_page),
            total_pages=total_pages,
        )


class HttpJsonSource:
    """带授权的 JSON 接口。

    配置中的 ``token_env`` 是环境变量名，令牌本身不写入数据库。
    通过 ``next_path`` 从响应中读取下一页游标；没有游标即表示最后一页。
    """

    def __init__(
        self,
        url: str,
        *,
        token_env: str | None = None,
        token_prefix: str = "Bearer ",
        records_path: str = "data",
        next_path: str = "next_cursor",
        total_path: str = "total_pages",
        page_param: str = "page",
        cursor_param: str = "cursor",
        page_size_param: str | None = None,
        page_size: int | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        headers: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
        line: str | None = None,
    ):
        self.url = url
        self.token_env = token_env
        self.token_prefix = token_prefix
        self.records_path = records_path
        self.next_path = next_path
        self.total_path = total_path
        self.page_param = page_param
        self.cursor_param = cursor_param
        self.page_size_param = page_size_param
        self.page_size = page_size
        self.timeout = int(timeout)
        self.headers = dict(headers or {})
        # 服务端查询参数，例如补传时只取某月的接口过滤条件。
        self.query = {str(k): str(v) for k, v in (query or {}).items()}
        self.filters = {
            key: value
            for key, value in (("period_from", period_from), ("period_to", period_to), ("line", line))
            if value not in (None, "")
        }

    def describe(self) -> dict[str, Any]:
        return {
            "kind": "http-json",
            "url": self.url,
            "token_env": self.token_env,
            "records_path": self.records_path,
            "next_path": self.next_path,
            "total_path": self.total_path,
            "page_param": self.page_param,
            "cursor_param": self.cursor_param,
            "page_size_param": self.page_size_param,
            "page_size": self.page_size,
            "timeout": self.timeout,
            "headers": self.headers,
            "query": self.query,
            **self.filters,
        }

    @staticmethod
    def _dig(value: Any, path: str | None) -> Any:
        if not path:
            return value
        current = value
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                return None
            current = current[part]
        return current

    def fetch(self, page: int, cursor: str | None) -> Page:
        query = dict(self.query)
        query[self.page_param] = str(page)
        if cursor is not None:
            query[self.cursor_param] = cursor
        if self.page_size is not None and self.page_size_param:
            query[self.page_size_param] = str(self.page_size)
        parts = urllib.parse.urlsplit(self.url)
        existing = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
        existing.update(query)
        url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(existing), parts.fragment)
        )
        request = urllib.request.Request(url, headers=dict(self.headers))
        request.add_header("Accept", "application/json")
        token = None
        if self.token_env:
            token = os.environ.get(self.token_env)
            if not token:
                raise ValidationError(f"缺少授权令牌环境变量 {self.token_env}")
            request.add_header("Authorization", f"{self.token_prefix}{token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                body = response.read().decode(charset)
        except Exception as exc:  # 网络错误统一包装，保留原因
            raise LedgerError(f"抓取 {url} 失败：{exc}") from exc
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"来源返回的不是合法 JSON：{url}") from exc
        records = self._dig(payload, self.records_path)
        if not isinstance(records, list):
            raise ValidationError(f"JSON 中的记录路径 {self.records_path!r} 不是数组")
        next_cursor = self._dig(payload, self.next_path)
        total = self._dig(payload, self.total_path)
        records = [dict(item) for item in records if isinstance(item, dict)]
        return Page(
            records=_filtered(records, self.filters),
            page=page,
            next_page=page + 1 if next_cursor not in (None, "") else None,
            next_cursor=None if next_cursor in (None, "") else str(next_cursor),
            total_pages=int(total) if total is not None else None,
        )


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(cell for cell in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            self._depth -= 1
            if self._depth == 0:
                self.tables.append(self._table)
                self._table = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


class HtmlTableSource:
    """公开因子页面：读取 HTML 中指定表格。

    ``columns`` 把页面表头映射为标准字段，例如
    ``{"因子": "factor", "来源": "source", "月份": "period", "数值": "value", "修订": "revision"}``。
    """

    def __init__(
        self,
        path_or_url: str,
        *,
        table_index: int = 0,
        columns: dict[str, str] | None = None,
        token_env: str | None = None,
        token_prefix: str = "Bearer ",
        timeout: int = DEFAULT_TIMEOUT,
        **filters: Any,
    ):
        self.path_or_url = path_or_url
        self.table_index = int(table_index)
        self.columns = dict(columns or {})
        self.token_env = token_env
        self.token_prefix = token_prefix
        self.timeout = int(timeout)
        self.filters = {k: v for k, v in filters.items() if v not in (None, "")}

    def describe(self) -> dict[str, Any]:
        return {
            "kind": "html-table",
            "path_or_url": self.path_or_url,
            "table_index": self.table_index,
            "columns": self.columns,
            "token_env": self.token_env,
            "timeout": self.timeout,
            **self.filters,
        }

    def _read(self) -> str:
        if self.path_or_url.startswith(("http://", "https://")):
            request = urllib.request.Request(self.path_or_url)
            request.add_header("Accept", "text/html")
            if self.token_env:
                token = os.environ.get(self.token_env)
                if not token:
                    raise ValidationError(f"缺少授权令牌环境变量 {self.token_env}")
                request.add_header("Authorization", f"{self.token_prefix}{token}")
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    charset = response.headers.get_content_charset() or "utf-8"
                    return response.read().decode(charset)
            except Exception as exc:
                raise LedgerError(f"抓取 {self.path_or_url} 失败：{exc}") from exc
        file = Path(self.path_or_url).expanduser()
        if not file.is_file():
            raise ValidationError(f"找不到 HTML 文件：{self.path_or_url}")
        return file.read_text(encoding="utf-8")

    def fetch(self, page: int, cursor: str | None) -> Page:
        if page != 1:
            raise ValidationError("HTML 表格来源不支持分页")
        parser = _TableParser()
        parser.feed(self._read())
        if not parser.tables:
            return Page(records=[], page=1, total_pages=1)
        try:
            table = parser.tables[self.table_index]
        except IndexError as exc:
            raise ValidationError(f"页面中没有第 {self.table_index} 张表格") from exc
        if not table:
            return Page(records=[], page=1, total_pages=1)
        headers = [cell.strip() for cell in table[0]]
        if self.columns:
            missing = [name for name in self.columns if name not in headers]
            if missing:
                raise ValidationError(f"因子页面缺少列：{', '.join(missing)}")
            headers = [self.columns.get(name, name) for name in headers]
        rows = []
        for raw in table[1:]:
            if len(raw) != len(headers):
                raise ValidationError("因子页面表格行列数不一致")
            rows.append(dict(zip(headers, raw)))
        rows = _filtered(rows, self.filters)
        return Page(records=rows, page=1, total_pages=1)


class JsonFileSource:
    """本地 JSON 导出（便于离线演练授权接口的响应）。"""

    def __init__(self, path: str, *, records_path: str = "data", next_path: str = "next_cursor",
                 total_path: str = "total_pages", page_size: int = 100, **filters: Any):
        self.path = str(Path(path).expanduser())
        self.records_path = records_path
        self.next_path = next_path
        self.total_path = total_path
        self.page_size = int(page_size)
        self.filters = {k: v for k, v in filters.items() if v not in (None, "")}

    def describe(self) -> dict[str, Any]:
        return {
            "kind": "json-file",
            "path": self.path,
            "records_path": self.records_path,
            "next_path": self.next_path,
            "total_path": self.total_path,
            "page_size": self.page_size,
            **self.filters,
        }

    def fetch(self, page: int, cursor: str | None) -> Page:
        payload = json.loads(Path(self.path).read_text(encoding="utf-8"))
        records = HttpJsonSource._dig(payload, self.records_path)
        if not isinstance(records, list):
            raise ValidationError(f"JSON 中的记录路径 {self.records_path!r} 不是数组")
        rows = _filtered([dict(r) for r in records if isinstance(r, dict)], self.filters)
        start = (page - 1) * self.page_size
        chunk = rows[start : start + self.page_size]
        total_pages = max(1, (len(rows) + self.page_size - 1) // self.page_size)
        next_page = page + 1 if page < total_pages else None
        return Page(records=chunk, page=page, next_page=next_page,
                    next_cursor=None if next_page is None else str(next_page),
                    total_pages=total_pages)


def build_source(config: dict[str, Any]) -> Source:
    kind = config.get("kind")
    if kind not in SOURCE_PARAMS:
        raise ValidationError(f"未知来源类型：{kind!r}")
    params = {k: v for k, v in config.items() if k != "kind"}
    _assert_no_inline_secrets(params)
    if kind == "html-table":
        # 命令行习惯用 path/url；统一成 path_or_url，且不接受同时给出两者。
        if "path_or_url" in params and ("path" in params or "url" in params):
            raise ValidationError("html-table 不能同时提供 path_or_url 与 path/url")
        if "path" in params and "url" in params:
            raise ValidationError("html-table 不能同时提供 path 和 url")
        if "path_or_url" not in params:
            if "path" in params:
                params["path_or_url"] = params.pop("path")
            elif "url" in params:
                params["path_or_url"] = params.pop("url")
    unknown = set(params) - SOURCE_PARAMS[kind]
    if unknown:
        raise ValidationError(
            f"{kind} 来源不支持参数：{', '.join(sorted(unknown))}；"
            f"支持：{', '.join(sorted(SOURCE_PARAMS[kind]))}"
        )
    if kind == "csv":
        return CsvSource(**params)
    if kind == "http-json":
        return HttpJsonSource(**params)
    if kind == "json-file":
        return JsonFileSource(**params)
    return HtmlTableSource(**params)


def rows_to_csv_text(rows: list[dict[str, Any]]) -> str:
    """测试与导出辅助。"""
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()
