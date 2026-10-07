import unittest
from unittest.mock import patch

import signal_tools as sg


class MathTests(unittest.TestCase):
    def test_balanced_table_is_one(self):
        self.assertAlmostEqual(sg.reporting_odds_ratio_math(10, 100, 110, 1100)["ror"], 1.0)

    def test_known_value(self):
        self.assertAlmostEqual(sg.reporting_odds_ratio_math(20, 100, 30, 200)["ror"], 2.25)

    def test_live_numbers(self):
        m = sg.reporting_odds_ratio_math(18314, 283545, 779375, 20692690)
        self.assertAlmostEqual(m["ror"], 1.7826, places=3)
        self.assertAlmostEqual(m["ci_low"], 1.7558, places=3)

    def test_error_paths(self):
        self.assertEqual(sg.reporting_odds_ratio_math(0, 100, 30, 200)["error"], "ZERO_CELL")
        self.assertEqual(sg.reporting_odds_ratio_math(500, 100, 30, 200)["error"], "INCONSISTENT_COUNTS")


class ToolTests(unittest.TestCase):
    def setUp(self):
        sg._cache.clear()

    def fake_totals(self, mapping):
        def _total(search):
            for needle, val in mapping:
                if needle is None and search is None: return val, None
                if needle and search and needle(search): return val, None
            return 0, None
        return _total

    def test_ror_end_to_end(self):
        t = self.fake_totals([
            (lambda s: " AND " in s, 18314),
            (lambda s: s.startswith("patient.drug"), 283545),
            (lambda s: s.startswith("patient.reaction"), 779375),
            (None, 20692690)])
        with patch.object(sg, "_total", t):
            r = sg.reporting_odds_ratio("Ibuprofen", "Nausea")
        self.assertEqual(r["reporting_odds_ratio"], 1.78)
        self.assertEqual(r["drug"], "ibuprofen")
        self.assertIn("caveats", r)

    def test_unknown_drug_is_actionable(self):
        with patch.object(sg, "_total", lambda s: (0, None)):
            self.assertEqual(sg.reporting_odds_ratio("zzzz", "nausea")["error"], "DRUG_NOT_FOUND")

    def test_unknown_reaction_is_actionable(self):
        calls = iter([(5, None), (100, None), (0, None)])
        with patch.object(sg, "_total", lambda s: next(calls)):
            r = sg.reporting_odds_ratio("ibuprofen", "upset tummy")
        self.assertEqual(r["error"], "REACTION_NOT_FOUND")
        self.assertIn("top_reactions", r["hint"])

    def test_input_is_sanitised(self):
        self.assertEqual(sg._clean('ibu"profen\\ AND x:y'), "ibuprofen AND xy")

    def test_missing_inputs(self):
        self.assertEqual(sg.reporting_odds_ratio("", "nausea")["error"], "MISSING_INPUT")
        self.assertEqual(sg.top_reactions("")["error"], "MISSING_DRUG")

    def test_assess_signal_known_effect(self):
        ror = {"drug": "ibuprofen", "reaction": "nausea", "reporting_odds_ratio": 1.78, "ci_95": [1.7, 1.8],
               "reports_with_drug_and_reaction": 100}
        lab = {"on_label": True, "sections": ["adverse_reactions"], "snippet": "...nausea..."}
        with patch.object(sg, "reporting_odds_ratio", return_value=ror), patch.object(sg, "check_label_for_reaction", return_value=lab):
            self.assertEqual(sg.assess_signal("ibuprofen", "nausea")["verdict"], "known_effect")

    def test_assess_signal_not_on_label(self):
        ror = {"drug": "d", "reaction": "r", "reporting_odds_ratio": 3.0, "ci_95": [2.0, 4.0], "reports_with_drug_and_reaction": 50}
        lab = {"on_label": False, "sections": [], "snippet": None}
        with patch.object(sg, "reporting_odds_ratio", return_value=ror), patch.object(sg, "check_label_for_reaction", return_value=lab):
            r = sg.assess_signal("d", "r")
        self.assertEqual(r["verdict"], "not_on_label_worth_asking_about")
        self.assertIn("not a finding", r["meaning"])

    def test_assess_signal_tiny_counts_not_a_signal(self):
        ror = {"drug": "d", "reaction": "r", "reporting_odds_ratio": 9.0, "ci_95": [2.0, 40.0], "reports_with_drug_and_reaction": 2}
        lab = {"on_label": False, "sections": [], "snippet": None}
        with patch.object(sg, "reporting_odds_ratio", return_value=ror), patch.object(sg, "check_label_for_reaction", return_value=lab):
            self.assertEqual(sg.assess_signal("d", "r")["verdict"], "no_signal")

    def test_label_check_finds_reaction(self):
        label = {"adverse_reactions": ["Common: Nausea, vomiting and dizziness were reported."], "warnings": ["Stomach bleeding."]}
        with patch.object(sg, "_label", return_value=(label, None)):
            r = sg.check_label_for_reaction("ibuprofen", "nausea")
        self.assertTrue(r["on_label"])
        self.assertEqual(r["sections"], ["adverse_reactions"])

    def test_label_prefers_single_ingredient_with_adverse_reactions(self):
        otc = {"openfda": {"generic_name": ["IBUPROFEN"]}, "warnings": ["Stomach bleeding."]}
        combo = {"openfda": {"generic_name": ["IBUPROFEN AND FAMOTIDINE"]}, "adverse_reactions": ["x"]}
        rx = {"openfda": {"generic_name": ["IBUPROFEN"]}, "adverse_reactions": ["Nausea."]}
        with patch.object(sg, "_get", return_value=({"results": [otc, combo, rx]}, None)):
            self.assertIs(sg._label("ibuprofen")[0], rx)

    def test_get_label_warnings_truncates(self):
        label = {"warnings": ["x" * 5000]}
        with patch.object(sg, "_label", return_value=(label, None)):
            r = sg.get_label_warnings("ibuprofen")
        self.assertLessEqual(len(r["warnings"]), 1203)

    def test_upstream_error_propagates(self):
        with patch.object(sg, "_total", lambda s: (None, {"error": "RATE_LIMITED", "hint": "wait"})):
            self.assertEqual(sg.reporting_odds_ratio("ibuprofen", "nausea")["error"], "RATE_LIMITED")

    def test_run_tool_never_raises(self):
        self.assertEqual(sg.run_tool("nope", {})["error"], "UNKNOWN_TOOL")
        self.assertEqual(sg.run_tool("assess_signal", {"bogus": 1})["error"], "BAD_ARGUMENTS")


if __name__ == "__main__":
    unittest.main()
