"""采集与分页续传测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from energy_ledger.errors import ConflictError, LedgerError, ValidationError
from energy_ledger.ledger import Ledger
from energy_ledger.sources import (
    CsvSource,
    HtmlTableSource,
    HttpJsonSource,
    JsonFileSource,
    build_source,
    rows_to_csv_text,
)

METER_COLUMNS = ["reading", "meter", "line", "period", "kwh", "version"]


def meter_rows(count: int) -> list[dict]:
    return [
        {
            "reading": f"R{i}",
            "meter": f"E-{i}",
            "line": "L-A",
            "period": "2026-09",
            "kwh": str(100 + i),
            "version": "v1",
        }
        for i in range(1, count + 1)
    ]


class SourceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, rows: list[dict], columns=None) -> Path:
        path = self.dir / name
        if columns:
            lines = [",".join(columns)]
            lines += [",".join(row.get(col, "") for col in columns) for row in rows]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            path.write_text(rows_to_csv_text(rows), encoding="utf-8")
        return path

    def test_csv_source_pages_and_filters(self):
        rows = meter_rows(5)
        rows.append({**rows[0], "reading": "R6", "period": "2026-08"})
        path = self.write("m.csv", rows)
        source = CsvSource(str(path), page_size=2, period_from="2026-09", period_to="2026-09")
        pages = []
        page, cursor = 1, None
        while True:
            result = source.fetch(page, cursor)
            pages.extend(result.records)
            if result.next_page is None:
                break
            page, cursor = result.next_page, result.next_cursor
        self.assertEqual(len(pages), 5)
        self.assertTrue(all(row["period"] == "2026-09" for row in pages))

    def test_csv_source_renames_nonstandard_columns(self):
        path = self.write(
            "m.csv",
            [{"读数号": "R1", "表号": "E-1", "产线": "L-A", "月份": "2026-09", "电量": "9", "版本": "v1"}],
            columns=["读数号", "表号", "产线", "月份", "电量", "版本"],
        )
        source = CsvSource(str(path), source_columns={
            "读数号": "reading", "表号": "meter", "产线": "line",
            "月份": "period", "电量": "kwh", "版本": "version",
        })
        row = source.fetch(1, None).records[0]
        self.assertEqual(row["meter"], "E-1")
        self.assertEqual(row["period"], "2026-09")

    def test_ambiguous_renamed_column_is_rejected(self):
        path = self.dir / "m.csv"
        path.write_text("reading,月份,period\nR1,2026-09,2026-09\n", encoding="utf-8")
        source = CsvSource(str(path), source_columns={"月份": "period"})
        with self.assertRaises(ValidationError):
            source.fetch(1, None)

    def test_json_file_source(self):
        payload = {"data": meter_rows(3), "total_pages": 1, "next_cursor": None}
        path = self.dir / "m.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        page = JsonFileSource(str(path), page_size=10).fetch(1, None)
        self.assertEqual(len(page.records), 3)
        self.assertIsNone(page.next_page)

    def test_html_table_source(self):
        html = """<html><body><table>
        <tr><th>因子</th><th>来源</th><th>月份</th><th>数值</th><th>修订</th></tr>
        <tr><td>F1</td><td>public-A</td><td>2026-09</td><td>0.73</td><td>2</td></tr>
        </table></body></html>"""
        path = self.dir / "f.html"
        path.write_text(html, encoding="utf-8")
        source = HtmlTableSource(
            str(path),
            columns={"因子": "factor", "来源": "source", "月份": "period", "数值": "value", "修订": "revision"},
        )
        rows = source.fetch(1, None).records
        self.assertEqual(rows[0]["factor"], "F1")
        self.assertEqual(rows[0]["value"], "0.73")

    def test_html_table_missing_column_is_rejected(self):
        html = "<table><tr><th>因子</th></tr><tr><td>F1</td></tr></table>"
        path = self.dir / "f.html"
        path.write_text(html, encoding="utf-8")
        source = HtmlTableSource(str(path), columns={"来源": "source"})
        with self.assertRaises(ValidationError):
            source.fetch(1, None)

    def test_inline_credentials_rejected(self):
        with self.assertRaises(ValidationError):
            build_source({"kind": "http-json", "url": "https://x.test", "headers": {"Api-Key": "x"}})

    def test_unknown_kind_or_parameter_rejected(self):
        with self.assertRaises(ValidationError):
            build_source({"kind": "ftp"})
        with self.assertRaises(ValidationError):
            build_source({"kind": "csv", "path": "x.csv", "page": 2})


class FakeSource:
    """可注入失败的分页来源，用于演练网络中断。"""

    def __init__(self, rows: list[dict], page_size: int, fail_on_page: int | None = None):
        self.rows = rows
        self.page_size = page_size
        self.fail_on_page = fail_on_page

    def describe(self):
        return {"kind": "fake", "page_size": self.page_size}

    def fetch(self, page: int, cursor: str | None):
        from energy_ledger.sources import Page

        if self.fail_on_page == page:
            raise OSError("模拟网络中断")
        start = (page - 1) * self.page_size
        chunk = self.rows[start : start + self.page_size]
        total = max(1, (len(self.rows) + self.page_size - 1) // self.page_size)
        next_page = page + 1 if page < total else None
        return Page(records=chunk, page=page, next_page=next_page,
                    next_cursor=None if next_page is None else str(next_page), total_pages=total)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.ledger = Ledger(self.dir / "l.db")

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def _source(self, rows, page_size=2, fail_on_page=None):
        fake = FakeSource(rows, page_size, fail_on_page)
        self.ledger._source_for_batch = lambda batch: fake
        return fake

    def test_interrupted_page_accepted_without_duplicates(self):
        rows = meter_rows(5)
        self._source(rows, page_size=2)
        batch_id = self.ledger.start_batch("meters", "fake", {"kind": "csv", "path": "unused"})
        first = self.ledger.run_batch(batch_id, max_pages=1)
        self.assertEqual(first["status"], "open")
        self.assertEqual(first["processed_pages"], 1)
        self.assertEqual(first["resume_page"], 2)
        summary = self.ledger.run_batch(batch_id)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(summary["records_stored"], 5)
        self.assertEqual(summary["pages_stored"], 3)

    def test_network_failure_marks_batch_interrupted_and_resumes(self):
        rows = meter_rows(6)
        self._source(rows, page_size=2, fail_on_page=2)
        batch_id = self.ledger.start_batch("meters", "fake", {"kind": "csv", "path": "unused"})
        with self.assertRaises(LedgerError) as caught:
            self.ledger.run_batch(batch_id)
        self.assertEqual(caught.exception.batch_id, batch_id)
        summary = self.ledger.batch_summary(batch_id)
        self.assertEqual(summary["status"], "interrupted")
        self.assertEqual(summary["next_page"], 2)
        self.assertEqual(summary["pages_stored"], 1)
        self.assertIn("模拟网络中断", summary["last_error"])

        self._source(rows, page_size=2, fail_on_page=None)
        resumed = self.ledger.run_batch(batch_id)
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(resumed["records_stored"], 6)

    def test_completed_batch_is_idempotent(self):
        self._source(meter_rows(3), page_size=2)
        batch_id = self.ledger.start_batch("meters", "fake", {"kind": "csv", "path": "unused"})
        self.ledger.run_batch(batch_id)
        again = self.ledger.run_batch(batch_id)
        self.assertEqual(again["records_stored"], 3)

    def test_changed_source_on_resume_is_conflict(self):
        rows = meter_rows(4)
        self._source(rows, page_size=2)
        batch_id = self.ledger.start_batch("meters", "fake", {"kind": "csv", "path": "unused"})
        self.ledger.run_batch(batch_id, max_pages=1)
        # 正常情况下已提交页不会重抓。这里把进度指回第 1 页，模拟来源在两次
        # 运行之间被替换：续传必须发现内容漂移并拒绝，而不是静默覆盖。
        rows[0] = {**rows[0], "kwh": "999"}
        self._source(rows, page_size=2)
        self.ledger.storage.execute(
            "UPDATE batches SET next_page = 1, cursor = '1' WHERE id = ?", (batch_id,)
        )
        with self.assertRaises(ConflictError):
            self.ledger.run_batch(batch_id)

    def test_validation_error_is_reported_as_data_error(self):
        rows = meter_rows(1)
        rows[0]["period"] = "2026-13"
        self._source(rows, page_size=1)
        batch_id = self.ledger.start_batch("meters", "fake", {"kind": "csv", "path": "unused"})
        with self.assertRaises(ValidationError):
            self.ledger.run_batch(batch_id)
        self.assertEqual(self.ledger.batch_summary(batch_id)["status"], "interrupted")

    def test_records_keep_batch_and_source_version(self):
        rows = meter_rows(1)
        self._source(rows, page_size=1)
        batch_id = self.ledger.start_batch("meters", "meter-api", {"kind": "csv", "path": "unused"})
        self.ledger.run_batch(batch_id)
        stored = self.ledger.list_observations("2026-09")["meter_observations"][0]
        self.assertEqual(stored["batch_id"], batch_id)
        self.assertEqual(stored["version"], "v1")
        self.assertTrue(stored["fetched_at"])


if __name__ == "__main__":
    unittest.main()
