#!/usr/bin/env python3
"""Transition Type is an enum dropdown, not two hard-coded buttons."""

from pathlib import Path
import unittest

import experience_store as xs

ROOT = Path(__file__).resolve().parent


class TheTypeDropdownIsAnEnum(unittest.TestCase):
    def setUp(self):
        self.js = (ROOT / "static" / "js" / "editor_graph.js").read_text(
            encoding="utf-8", errors="replace")

    def test_client_catalog_matches_the_store(self):
        blob = self.js.split("const TRANSITION_TYPES = [", 1)[1].split("];", 1)[0]
        for row in xs.condition_catalog():
            self.assertIn('id: "' + row["id"] + '"', blob)
            self.assertIn('label: "' + row["label"] + '"', blob)

    def test_inspector_renders_a_type_select_from_the_catalog(self):
        insp = self.js.split("function openLinkInspector", 1)[1][:2400]
        self.assertIn("transitionTypes()", insp)
        self.assertIn('textContent = "Type"', insp)
        self.assertIn("eg-select", insp)
        self.assertNotIn("modeBtn", insp)
        self.assertNotIn("eg-insp-mode", insp)

    def test_the_sheet_uses_the_same_enum(self):
        sheet = self.js.split("function sheetTransition", 1)[1][:900]
        self.assertIn('selectRow(body, "Type"', sheet)
        self.assertIn("transitionTypes()", sheet)
        self.assertNotIn('"When"', sheet)


if __name__ == "__main__":
    unittest.main(verbosity=2)
