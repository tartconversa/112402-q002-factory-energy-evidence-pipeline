"""核算规则测试：可发布条件、迁移、返工、因子修订与发布冻结。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from energy_ledger.errors import ConflictError, NotReadyError, ValidationError
from energy_ledger.ledger import Ledger
from energy_ledger.sources import rows_to_csv_text
from energy_ledger.util import period_end

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def write_csv(directory: Path, name: str, rows: list[dict]) -> Path:
    path = directory / name
    path.write_text(rows_to_csv_text(rows), encoding="utf-8")
    return path


def meter_reading(reading, meter, line, period, kwh, version):
    return {"reading": reading, "meter": meter, "line": line, "period": period,
            "kwh": "" if kwh is None else str(kwh), "version": version}


def production_row(row_id, line, period, units, scope="final", version=""):
    row = {"row_id": row_id, "line": line, "period": period,
           "good_units": "" if units is None else str(units), "scope": scope}
    if version:
        row["version"] = version
    return row


def factor_row(factor, period, value, revision, source="public-A", **extra):
    row = {"factor": factor, "source": source, "period": period,
           "value": "" if value is None else str(value), "revision": str(revision)}
    row.update({k: str(v) for k, v in extra.items()})
    return row


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.db = self.dir / "ledger.db"
        self.ledger = Ledger(self.db)

    def tearDown(self):
        self.ledger.close()
        self._tmp.cleanup()

    def ingest(self, source, rows, name=None, origin="test", **kwargs):
        path = write_csv(self.dir, name or f"{source}.csv", rows)
        return self.ledger.fetch(source, origin, config={"kind": "csv", "path": str(path)}, **kwargs)

    def base_month(self, period="2026-09", meter_kwh=8700, units=1000, factor=0.7):
        self.ingest("meters", [meter_reading("R1", "E-1", "L-A", period, meter_kwh, "v1")])
        self.ingest("production", [production_row("P1", "L-A", period, units)])
        self.ingest("factors", [factor_row("F1", period, factor, 1)])
        self.ledger.confirm_meter_mapping("E-1", "L-A", f"{period}-01", period_end(period))
        self.ledger.confirm_production_scope("L-A", f"{period}-01", period_end(period))

    def line(self, draft, name="L-A"):
        return next(item for item in draft["lines"] if item["line"] == name)

    def codes(self, draft, name="L-A"):
        return {issue["code"] for issue in self.line(draft, name)["issues"]}


class ReadyConditions(LedgerCase):
    def test_ready_metric_uses_latest_versions_and_factor(self):
        self.base_month()
        draft = self.ledger.compute("2026-09")
        item = self.line(draft)
        self.assertEqual(item["status"], "ready")
        self.assertAlmostEqual(item["metric"]["energy_kwh"], 8700)
        self.assertAlmostEqual(item["metric"]["good_units"], 1000)
        self.assertAlmostEqual(item["metric"]["converted_per_unit"], 8700 * 0.7 / 1000)

    def test_missing_mapping_blocks_and_is_not_allocated(self):
        self.base_month()
        self.ledger.storage.execute("DELETE FROM meter_mappings")
        draft = self.ledger.compute("2026-09")
        self.assertIn("meter_unmapped", self.codes(draft))
        self.assertIsNone(self.line(draft)["metric"])
        self.assertEqual(draft["comparable_lines"], [])

    def test_partial_and_pending_mappings_block(self):
        for status in ("partial", "pending"):
            with self.subTest(status=status):
                self.ledger.close()
                self._tmp.cleanup()
                self.setUp()
                self.ingest("meters", [meter_reading("R1", "E-1", "L-A", "2026-09", 100, "v1")])
                self.ingest("production", [production_row("P1", "L-A", "2026-09", 10)])
                self.ingest("factors", [factor_row("F1", "2026-09", 0.7, 1)])
                self.ledger.confirm_meter_mapping(
                    "E-1", "L-A", "2026-09-01", "2026-09-30", status=status
                )
                self.ledger.confirm_production_scope("L-A", "2026-09-01", "2026-09-30")
                self.assertIn(f"meter_mapping_{status}", self.codes(self.ledger.compute("2026-09")))

    def test_multiple_readings_for_one_meter_are_summed(self):
        self.ingest("meters", [
            meter_reading("R1a", "E-1", "L-A", "2026-09", 4000, "v1"),
            meter_reading("R1b", "E-1", "L-A", "2026-09", 4700, "v1"),
        ])
        self.ingest("production", [production_row("P1", "L-A", "2026-09", 1000)])
        self.ingest("factors", [factor_row("F1", "2026-09", 0.7, 1)])
        self.ledger.confirm_meter_mapping("E-1", "L-A", "2026-09-01", "2026-09-30")
        self.ledger.confirm_production_scope("L-A", "2026-09-01", "2026-09-30")
        item = self.line(self.ledger.compute("2026-09"))
        self.assertEqual(item["status"], "ready")
        self.assertAlmostEqual(item["metric"]["energy_kwh"], 8700)
        self.assertEqual(len(item["evidence"]["meters"]), 2)

    def test_correction_replaces_only_the_matching_reading(self):
        self.ingest("meters", [
            meter_reading("R1a", "E-1", "L-A", "2026-09", 4000, "v1"),
            meter_reading("R1b", "E-1", "L-A", "2026-09", 4700, "v1"),
        ])
        self.ingest("meters", [meter_reading("R1a", "E-1", "L-A", "2026-09", 4100, "v2")])
        self.ingest("production", [production_row("P1", "L-A", "2026-09", 1000)])
        self.ingest("factors", [factor_row("F1", "2026-09", 0.7, 1)])
        self.ledger.confirm_meter_mapping("E-1", "L-A", "2026-09-01", "2026-09-30")
        self.ledger.confirm_production_scope("L-A", "2026-09-01", "2026-09-30")
        item = self.line(self.ledger.compute("2026-09"))
        self.assertAlmostEqual(item["metric"]["energy_kwh"], 4100 + 4700)
        versions = {o["reading"]: o["version"] for o in item["evidence"]["meters"]}
        self.assertEqual(versions, {"R1a": "v2", "R1b": "v1"})

    def test_same_reading_same_version_conflict(self):
        self.base_month()
        with self.assertRaises(ConflictError):
            self.ingest("meters", [meter_reading("R1", "E-1", "L-A", "2026-09", 1, "v1")])

    def test_empty_reading_is_never_filled_from_other_period(self):
        self.ingest("meters", [
            meter_reading("R1", "E-1", "L-A", "2026-08", 1000, "v1"),
            meter_reading("R2", "E-1", "L-A", "2026-09", None, "v2"),
        ])
        self.ingest("production", [
            production_row("P1", "L-A", "2026-08", 100),
            production_row("P2", "L-A", "2026-09", 90),
        ])
        self.ingest("factors", [
            factor_row("F1", "2026-08", 0.7, 1),
            factor_row("F2", "2026-09", 0.7, 1),
        ])
        self.ledger.confirm_meter_mapping("E-1", "L-A", "2026-08-01", "2026-09-30")
        self.ledger.confirm_production_scope("L-A", "2026-08-01", "2026-09-30")
        draft = self.ledger.compute("2026-09")
        self.assertIn("meter_reading_missing", self.codes(draft))
        self.assertIsNone(self.line(draft)["metric"])

    def test_empty_production_is_never_filled_from_other_period(self):
        self.base_month()
        self.ingest("production", [production_row("P1", "L-A", "2026-09", None, version="v2")])
        draft = self.ledger.compute("2026-09")
        self.assertIn("production_missing", self.codes(draft))
        self.assertIsNone(self.line(draft)["metric"])

    def test_unconfirmed_scope_blocks(self):
        self.base_month()
        self.ledger.storage.execute("DELETE FROM production_scopes")
        self.assertIn("production_scope_unconfirmed", self.codes(self.ledger.compute("2026-09")))

    def test_scope_only_covering_part_of_month_blocks(self):
        self.base_month()
        self.ledger.storage.execute("DELETE FROM production_scopes")
        self.ledger.confirm_production_scope("L-A", "2026-09-15", "2026-09-30")
        self.assertIn("production_scope_partial_period", self.codes(self.ledger.compute("2026-09")))


class MeterMigration(LedgerCase):
    def test_change_of_meter_is_resolved_by_dated_mapping(self):
        self.ingest("meters", [
            meter_reading("R1", "E-OLD", "L-A", "2026-08", 8000, "v1"),
            meter_reading("R2", "E-NEW", "L-A", "2026-09", 9000, "v1"),
        ])
        self.ingest("production", [
            production_row("P1", "L-A", "2026-08", 800),
            production_row("P2", "L-A", "2026-09", 1000),
        ])
        self.ingest("factors", [
            factor_row("F1", "2026-08", 0.7, 1),
            factor_row("F2", "2026-09", 0.7, 1),
        ])
        self.ledger.confirm_meter_mapping("E-OLD", "L-A", "2026-08-01", "2026-08-31")
        self.ledger.confirm_meter_mapping(
            "E-NEW", "L-A", "2026-09-01", "2026-12-31", replaces_meter="E-OLD"
        )
        self.ledger.confirm_production_scope("L-A", "2026-08-01", "2026-12-31")
        august = self.line(self.ledger.compute("2026-08"))
        september = self.line(self.ledger.compute("2026-09"))
        self.assertEqual(august["status"], "ready")
        self.assertEqual(september["status"], "ready")
        self.assertAlmostEqual(august["metric"]["energy_kwh"], 8000)
        self.assertAlmostEqual(september["metric"]["energy_kwh"], 9000)
        self.assertEqual(
            september["evidence"]["meters"][0]["mapping"]["replaces_meter"], "E-OLD"
        )

    def test_mapping_and_observation_line_mismatch_blocks_both(self):
        self.base_month()
        self.ledger.storage.execute(
            "UPDATE meter_mappings SET line = 'L-B'"
        )
        draft = self.ledger.compute("2026-09")
        self.assertIn("meter_line_mismatch", self.codes(draft, "L-A") | self.codes(draft, "L-B"))
        self.assertEqual(draft["comparable_lines"], [])

    def test_change_of_meter_mid_month_without_gap_is_ready(self):
        self.ingest("meters", [
            meter_reading("R1", "E-OLD", "L-A", "2026-09", 4000, "v1"),
            meter_reading("R2", "E-NEW", "L-A", "2026-09", 5000, "v1"),
        ])
        self.ingest("production", [production_row("P1", "L-A", "2026-09", 1000)])
        self.ingest("factors", [factor_row("F1", "2026-09", 0.7, 1)])
        self.ledger.confirm_meter_mapping("E-OLD", "L-A", "2026-09-01", "2026-09-14")
        self.ledger.confirm_meter_mapping(
            "E-NEW", "L-A", "2026-09-15", "2026-09-30", replaces_meter="E-OLD"
        )
        self.ledger.confirm_production_scope("L-A", "2026-09-01", "2026-09-30")
        item = self.line(self.ledger.compute("2026-09"))
        self.assertEqual(item["status"], "ready")
        self.assertAlmostEqual(item["metric"]["energy_kwh"], 9000)

    def test_meter_with_mapping_but_no_reading_blocks(self):
        self.base_month()
        self.ledger.confirm_meter_mapping("E-2", "L-A", "2026-09-01", "2026-09-30")
        self.assertIn("meter_observation_missing", self.codes(self.ledger.compute("2026-09")))

    def test_change_of_meter_with_coverage_gap_blocks(self):
        self.ingest("meters", [
            meter_reading("R1", "E-OLD", "L-A", "2026-09", 4000, "v1"),
            meter_reading("R2", "E-NEW", "L-A", "2026-09", 5000, "v1"),
        ])
        self.ingest("production", [production_row("P1", "L-A", "2026-09", 1000)])
        self.ingest("factors", [factor_row("F1", "2026-09", 0.7, 1)])
        self.ledger.confirm_meter_mapping("E-OLD", "L-A", "2026-09-01", "2026-09-10")
        self.ledger.confirm_meter_mapping("E-NEW", "L-A", "2026-09-15", "2026-09-30")
        self.ledger.confirm_production_scope("L-A", "2026-09-01", "2026-09-30")
        item = self.line(self.ledger.compute("2026-09"))
        self.assertIn("meter_mapping_gap", {i["code"] for i in item["issues"]})
        self.assertIsNone(item["metric"])

    def test_overlapping_mappings_are_rejected(self):
        self.ledger.confirm_meter_mapping("E-1", "L-A", "2026-09-01", "2026-12-31")
        with self.assertRaises(ConflictError):
            self.ledger.confirm_meter_mapping("E-1", "L-B", "2026-10-01", "2027-03-31")

    def test_supersede_shortens_old_mapping_and_keeps_audit(self):
        first = self.ledger.confirm_meter_mapping("E-1", "L-A", "2026-09-01", "2026-12-31")
        second = self.ledger.confirm_meter_mapping(
            "E-1", "L-B", "2026-11-01", "2026-12-31", supersedes_id=first
        )
        rows = {row["id"]: row for row in self.ledger.list_meter_mappings("E-1")}
        self.assertEqual(rows[first]["valid_to"], "2026-10-31")
        self.assertEqual(rows[second]["supersedes_id"], first)
        actions = [row["action"] for row in self.ledger.audit_log()]
        self.assertIn("mapping.confirm", actions)

    def test_retract_keeps_history_and_blocks(self):
        self.base_month()
        mapping_id = self.ledger.list_meter_mappings("E-1")[0]["id"]
        self.ledger.retract_meter_mapping(mapping_id, reason="换表核对")
        self.assertIn("meter_mapping_pending", self.codes(self.ledger.compute("2026-09")))


class ProductionScope(LedgerCase):
    def test_rework_in_source_blocks_until_corrected(self):
        self.ingest("meters", [meter_reading("R1", "E-1", "L-A", "2026-09", 1000, "v1")])
        self.ingest("production", [production_row("P1", "L-A", "2026-09", 100, scope="includes-rework")])
        self.ingest("factors", [factor_row("F1", "2026-09", 0.7, 1)])
        self.ledger.confirm_meter_mapping("E-1", "L-A", "2026-09-01", "2026-09-30")
        self.ledger.confirm_production_scope("L-A", "2026-09-01", "2026-09-30", scope="final")
        codes = self.codes(self.ledger.compute("2026-09"))
        self.assertIn("production_scope_not_final", codes)
        self.assertIn("production_scope_mismatch", codes)

    def test_revised_good_units_recorded_as_version(self):
        self.base_month()
        self.ingest("production", [
            production_row("P1", "L-A", "2026-09", 1100, version="v2")
        ])
        draft = self.ledger.compute("2026-09")
        self.assertAlmostEqual(self.line(draft)["metric"]["good_units"], 1100)
        evidence = self.line(draft)["evidence"]["production"][0]
        self.assertEqual(evidence["version"], "v2")
        history = self.ledger.trace("2026-09", "L-A")["raw"]["production_observations"]
        self.assertEqual([row["version"] for row in history], ["", "v2"])

    def test_same_version_different_content_conflicts(self):
        self.base_month()
        with self.assertRaises(ConflictError):
            self.ingest("production", [
                production_row("P1", "L-A", "2026-09", 1200, version="v2")
            ])
            self.ingest("production", [
                production_row("P1", "L-A", "2026-09", 1300, version="v2")
            ])


class Factors(LedgerCase):
    def test_highest_revision_wins_and_is_traceable(self):
        self.base_month(factor=0.7)
        self.ingest("factors", [factor_row("F2", "2026-09", 0.75, 2)])
        item = self.line(self.ledger.compute("2026-09"))
        self.assertAlmostEqual(item["metric"]["factor_value"], 0.75)
        applied = item["evidence"]["factor"]["applied"]
        self.assertEqual(applied["revision"], 2)

    def test_retroactive_revision_only_changes_unpublished_or_via_impact(self):
        self.base_month(factor=0.7)
        release = self.ledger.publish("2026-09")
        self.ingest("factors", [factor_row("F2", "2026-09", 0.8, 2)])
        impact = self.ledger.impact("2026-09")
        self.assertTrue(impact["changed"])
        self.assertTrue(impact["needs_reissue"])
        self.assertIn("L-A", impact["reissue_scope"]["lines"])
        old = self.ledger.get_release("2026-09", release["revision"])
        self.assertAlmostEqual(
            old["payload"]["draft"]["lines"][0]["metric"]["factor_value"], 0.7
        )
        self.assertEqual(old["payload_hash"], release["payload_hash"])

    def test_factor_revision_without_full_month_coverage_blocks(self):
        self.base_month(factor=0.7)
        self.ingest("factors", [
            factor_row("F2", "2026-09", 0.8, 2, effective_from="2026-09-15",
                       effective_to="2026-09-18")
        ])
        item = self.line(self.ledger.compute("2026-09"))
        # 月中生效的修订不能用整月套用，仍使用整月覆盖的 r1。
        self.assertEqual(item["status"], "ready")
        self.assertEqual(item["evidence"]["factor"]["applied"]["revision"], 1)

    def test_factor_from_other_period_is_not_borrowed(self):
        self.base_month(factor=0.7)
        self.ledger.storage.execute("DELETE FROM factor_observations WHERE period = '2026-09'")
        self.assertIn("factor_missing", self.codes(self.ledger.compute("2026-09")))

    def test_multiple_sources_require_explicit_selection(self):
        self.base_month(factor=0.7)
        self.ingest("factors", [factor_row("F9", "2026-09", 0.5, 1, source="public-B")])
        draft = self.ledger.compute("2026-09")
        self.assertIn("factor_ambiguous", self.codes(draft))
        self.ledger.set_factor_selection("public-B", "", period="2026-09")
        item = self.line(self.ledger.compute("2026-09"))
        self.assertEqual(item["status"], "ready")
        self.assertAlmostEqual(item["metric"]["factor_value"], 0.5)

    def test_factor_value_missing_does_not_fall_back(self):
        self.base_month(factor=0.7)
        self.ingest("factors", [factor_row("F2", "2026-09", None, 2)])
        self.assertIn("factor_value_missing", self.codes(self.ledger.compute("2026-09")))

    def test_duplicate_revision_with_different_value_blocks(self):
        self.base_month(factor=0.7)
        # 同一修订号可以按不同来源期间分别登记（例如季度覆盖），但若它们同时
        # 覆盖本月且数值不同，就无法判断哪条有效。
        self.ledger.storage.execute(
            "INSERT INTO factor_observations (batch_id, factor, source, factor_key, period, "
            "value, revision, effective_from, effective_to, raw_json, payload_hash, fetched_at) "
            "VALUES (1, 'F3', 'public-A', '', '2026-10', 0.9, 1, '2026-07-01', '2026-12-31', "
            "'{}', 'different', '2026-09-30T00:00:00+00:00')"
        )
        self.assertIn("factor_revision_ambiguous", self.codes(self.ledger.compute("2026-09")))


class Publishing(LedgerCase):
    def test_publish_requires_ready_lines(self):
        self.ingest("meters", [meter_reading("R1", "E-1", "L-A", "2026-09", 100, "v1")])
        with self.assertRaises(NotReadyError):
            self.ledger.publish("2026-09")

    def test_partial_publish_keeps_incomparable_reasons(self):
        self.base_month()
        self.ingest("meters", [meter_reading("R2", "E-9", "L-B", "2026-09", 500, "v1")])
        with self.assertRaises(NotReadyError):
            self.ledger.publish("2026-09")
        release = self.ledger.publish("2026-09", allow_partial=True, note="L-B 待计量确认")
        draft = release["payload"]["draft"]
        self.assertEqual(draft["comparable_lines"], ["L-A"])
        self.assertEqual(draft["incomparable"][0]["line"], "L-B")
        self.assertTrue(draft["incomparable"][0]["reasons"])

    def test_release_is_frozen_and_revisioned(self):
        self.base_month()
        first = self.ledger.publish("2026-09", published_by="alice")
        self.ingest("meters", [
            meter_reading("R2", "E-1", "L-A", "2026-09", 9000, "v2")
        ])
        second = self.ledger.publish("2026-09", published_by="bob", note="电表更正")
        self.assertEqual(second["revision"], first["revision"] + 1)
        self.assertEqual(second["supersedes_id"], first["id"])
        still_first = self.ledger.get_release("2026-09", 1)
        self.assertAlmostEqual(
            still_first["payload"]["draft"]["lines"][0]["metric"]["energy_kwh"], 8700
        )
        self.assertNotEqual(still_first["payload_hash"], second["payload_hash"])

    def test_release_detects_tampering(self):
        self.base_month()
        release = self.ledger.publish("2026-09")
        self.ledger.storage.execute(
            "UPDATE releases SET payload_json = '{}' WHERE id = ?", (release["id"],)
        )
        with self.assertRaises(ConflictError):
            self.ledger.get_release("2026-09")

    def test_impact_reports_rank_and_park_changes(self):
        self.base_month()
        self.ingest("meters", [meter_reading("R2", "E-2", "L-B", "2026-09", 5000, "v1")])
        self.ingest("production", [production_row("P2", "L-B", "2026-09", 1000)])
        self.ledger.confirm_meter_mapping("E-2", "L-B", "2026-09-01", "2026-09-30")
        self.ledger.confirm_production_scope("L-B", "2026-09-01", "2026-09-30")
        self.ledger.publish("2026-09")
        # L-B 单位能耗更低，发布时排名第一。
        self.assertEqual(self.ledger.compute("2026-09")["ranking"][0]["line"], "L-B")
        # L-B 电表更正后能耗大增，L-A 应升至第一。
        self.ingest("meters", [meter_reading("R3", "E-2", "L-B", "2026-09", 50000, "v2")])
        impact = self.ledger.impact("2026-09")
        self.assertTrue(impact["changed"])
        self.assertTrue(any(
            change.get("line") == "L-B" for change in impact["ranking_changes"]
        ))
        self.assertGreater(
            impact["park_summary"]["after"]["converted_per_unit"],
            impact["park_summary"]["before"]["converted_per_unit"],
        )

    def test_unpublished_impact_is_flagged(self):
        impact = self.ledger.impact("2026-09")
        self.assertFalse(impact["needs_reissue"])
        self.assertIn("尚未发布", impact["message"])


class Comparison(LedgerCase):
    def test_compare_blocks_when_either_month_not_ready(self):
        self.base_month("2026-08", 8000, 800)
        self.ingest("meters", [meter_reading("R2", "E-1", "L-A", "2026-09", 9000, "v2")])
        self.ingest("production", [production_row("P2", "L-A", "2026-09", 1000)])
        self.ingest("factors", [factor_row("F2", "2026-09", 0.7, 1)])
        result = self.ledger.compare_periods("L-A", "2026-08", "2026-09")
        self.assertFalse(result["comparable"])
        self.assertTrue(result["reasons"])

    def test_compare_ready_months(self):
        self.base_month("2026-08", 8000, 800)
        self.ingest("meters", [meter_reading("R2", "E-1", "L-A", "2026-09", 9000, "v2")])
        self.ingest("production", [production_row("P2", "L-A", "2026-09", 1000)])
        self.ingest("factors", [factor_row("F2", "2026-09", 0.7, 1)])
        self.ledger.confirm_meter_mapping("E-1", "L-A", "2026-09-01", "2026-09-30")
        self.ledger.confirm_production_scope("L-A", "2026-09-01", "2026-09-30")
        result = self.ledger.compare_periods("L-A", "2026-08", "2026-09")
        self.assertTrue(result["comparable"])
        self.assertAlmostEqual(result["a"]["metric"]["converted_per_unit"], 7.0)
        self.assertAlmostEqual(result["b"]["metric"]["converted_per_unit"], 6.3)


class Validation(LedgerCase):
    def test_inline_secret_is_rejected(self):
        with self.assertRaises(ValidationError):
            self.ledger.fetch(
                "meters", "api",
                config={"kind": "http-json", "url": "https://example.test", "token": "x"},
            )

    def test_unknown_config_key_is_rejected(self):
        path = write_csv(self.dir, "m.csv", [meter_reading("R1", "E-1", "L-A", "2026-09", 1, "v1")])
        with self.assertRaises(ValidationError):
            self.ledger.fetch("meters", "x", config={"kind": "csv", "path": str(path), "token_env": "T"})

    def test_bad_period_is_rejected_at_ingest(self):
        with self.assertRaises(ValidationError):
            self.ingest("meters", [meter_reading("R1", "E-1", "L-A", "2026-13", 1, "v1")])

    def test_trace_exposes_raw_observations_and_caliber(self):
        self.base_month()
        trace = self.ledger.trace("2026-09", "L-A")
        self.assertEqual(trace["metric"]["status"], "ready")
        self.assertEqual(len(trace["raw"]["meter_observations"]["E-1"]), 1)
        self.assertEqual(len(trace["raw"]["production_observations"]), 1)
        self.assertTrue(trace["raw"]["factor_observations"])


if __name__ == "__main__":
    unittest.main()
