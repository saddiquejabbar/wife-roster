from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from roster.extractor import extract_candidate
from roster.fragments import attribute_row_dates
from roster.models import RawTranscription

from helpers import build, fly, transcription


class FakeTranscriber:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def transcribe(self, sources, pdf_text_layers, prompt_path):
        self.calls.append((tuple(sources), pdf_text_layers, prompt_path))
        return self.payload


class TranscriptionTests(unittest.TestCase):
    def test_standard_fly_row(self):
        roster = build([fly("06Aug37", "ZX 410", "SIN-TPE", "0945", "1145", "1630")])
        self.assertEqual([sector.flight_number for sector in roster.sectors], ["ZX410"])
        self.assertTrue(roster.sectors[0].valid)

    def test_several_images_are_one_candidate(self):
        payload = {
            "schema_version": 1,
            "coverage": "FULL",
            "report_header": {
                "period_from": "01Aug37",
                "period_to": "01Oct37",
                "port_local_notice_present": True,
            },
            "rows": [],
        }
        backend = FakeTranscriber(payload)
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "page1.png"
            second = Path(directory) / "page2.jpg"
            first.write_bytes(b"synthetic png")
            second.write_bytes(b"synthetic jpg")
            result = extract_candidate([first, second], transcriber=backend)
        self.assertIsInstance(result, RawTranscription)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(len(backend.calls[0][0]), 2)

    def test_blank_cells_are_preserved(self):
        raw = transcription(
            [
                {
                    "start_date": "06Aug37",
                    "flight_number": "ZX 410",
                    "sector": "SIN-TPE",
                    "duty": "FLY",
                }
            ]
        )
        self.assertIsNone(raw.rows[0].rpt)
        self.assertIsNone(raw.rows[0].std)
        self.assertIsNone(raw.rows[0].remarks)

    def test_direct_json_transcription_source(self):
        value = {
            "schema_version": 1,
            "coverage": "PARTIAL",
            "report_header": {
                "period_from": "01Aug37",
                "period_to": "01Oct37",
                "port_local_notice_present": True,
            },
            "rows": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            result = extract_candidate([path])
        self.assertEqual(result.coverage.value, "PARTIAL")

    def test_pdf_text_layer_is_preferred_when_usable(self):
        payload = {
            "schema_version": 1,
            "coverage": "FULL",
            "report_header": {
                "period_from": "01Aug37",
                "period_to": "01Oct37",
                "port_local_notice_present": True,
            },
            "rows": [],
        }
        backend = FakeTranscriber(payload)
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "roster.pdf"
            pdf.write_bytes(b"%PDF synthetic")
            with patch("roster.extractor.extract_pdf_text_layer", return_value="RPT STD STA FLY roster text"):
                extract_candidate([pdf], transcriber=backend)
        self.assertEqual(backend.calls[0][1], {0: "RPT STD STA FLY roster text"})

    def test_pdf_without_text_layer_uses_visual_boundary(self):
        payload = {
            "schema_version": 1,
            "coverage": "FULL",
            "report_header": {
                "period_from": "01Aug37",
                "period_to": "01Oct37",
                "port_local_notice_present": True,
            },
            "rows": [],
        }
        backend = FakeTranscriber(payload)
        with tempfile.TemporaryDirectory() as directory:
            pdf = Path(directory) / "scan.pdf"
            pdf.write_bytes(b"%PDF synthetic scan")
            with patch("roster.extractor.extract_pdf_text_layer", return_value=None):
                extract_candidate([pdf], transcriber=backend)
        self.assertEqual(backend.calls[0][1], {})

    def test_pinned_schema_rejects_unknown_top_level_fields(self):
        with self.assertRaisesRegex(ValueError, "unknown transcription fields"):
            RawTranscription.from_dict(
                {
                    "schema_version": 1,
                    "coverage": "FULL",
                    "report_header": {
                        "period_from": "01Aug37",
                        "period_to": "01Oct37",
                        "port_local_notice_present": True,
                    },
                    "rows": [],
                    "invented": True,
                }
            )

    def test_undated_row_inherits_nearest_date_above(self):
        raw = transcription(
            [
                {"start_date": "07Aug37", "duty": "LO"},
                {
                    "flight_number": "ZX411",
                    "sector": "TPE-SIN",
                    "duty": "FLY",
                    "rpt": "1645",
                    "std": "1745",
                    "sta": "2215",
                },
            ]
        )
        rows, issues = attribute_row_dates(raw)
        self.assertFalse(issues)
        self.assertEqual(rows[1].row_date.isoformat(), "2037-08-07")
        roster = build(
            [
                {"start_date": "07Aug37", "duty": "LO"},
                {
                    "flight_number": "ZX411",
                    "sector": "TPE-SIN",
                    "duty": "FLY",
                    "rpt": "1645",
                    "std": "1745",
                    "sta": "2215",
                },
            ]
        )
        self.assertEqual(roster.sectors[0].std_date.isoformat(), "2037-08-07")

    def test_flight_split_across_rows_and_dates(self):
        roster = build(
            [
                fly("05Sep37", "ZX511", "BLR-SIN", "2205", "2305", None),
                {
                    "start_date": "06Sep37",
                    "flight_number": "ZX511",
                    "sector": "BLR-SIN",
                    "duty": "FLY",
                    "sta": "0610",
                },
            ]
        )
        self.assertEqual(len(roster.sectors), 1)
        sector = roster.sectors[0]
        self.assertEqual(sector.std_date.isoformat(), "2037-09-05")
        self.assertEqual(sector.sta_date.isoformat(), "2037-09-06")
        self.assertEqual(len(sector.source_positions), 2)

    def test_sta_only_blank_duty_continuation(self):
        roster = build(
            [
                fly("05Sep37", "ZX511", "BLR-SIN", "2205", "2305", None),
                {
                    "start_date": "06Sep37",
                    "flight_number": "ZX511",
                    "sector": "BLR-SIN",
                    "duty": None,
                    "sta": "0610",
                },
            ]
        )
        self.assertEqual(len(roster.sectors), 1)
        self.assertEqual(roster.sectors[0].sta_printed, "0610")

    def test_multi_sector_duty_and_second_sector_without_rpt(self):
        roster = build(
            [
                fly("12Aug37", "ZX910", "SIN-MNL", "0650", "0850", "1245"),
                fly("12Aug37", "ZX917", "MNL-SIN", None, "1400", "1750"),
            ]
        )
        self.assertEqual(len(roster.duties), 1)
        self.assertEqual([s.flight_number for s in roster.duties[0].sectors], ["ZX910", "ZX917"])
        self.assertEqual(roster.duties[0].rpt_printed, "0650")

    def test_timed_non_fly_and_unknown_codes_are_ignored(self):
        rows = []
        for index, duty in enumerate(("AALV", "STBY", "SS22", "MYSTERY")):
            rows.append(
                {
                    "start_date": f"{index + 1:02d}Aug37",
                    "flight_number": f"ZX{800 + index}",
                    "sector": "SIN-TPE",
                    "duty": duty,
                    "rpt": "0900",
                    "std": "1000",
                    "sta": "1400",
                }
            )
        roster = build(rows)
        self.assertEqual(roster.sectors, [])


if __name__ == "__main__":
    unittest.main()
