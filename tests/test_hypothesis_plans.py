import json
import unittest

from alpha_loop.hypothesis_plans import render_plans, validate_plans


class HypothesisPlansTest(unittest.TestCase):
    def content(self, ids=None, **extra):
        return json.dumps({"hypotheses": [{"condition_ids": ids or ["volume_ge_1_5", "near_high_ge_minus_0_05"], "reason_code": "volume_expansion", **extra}]})

    def test_conditions_units_and_comparison_are_defined_by_python(self):
        plans = validate_plans(self.content(), "study-test", "synthetic")
        plan = plans[0]
        self.assertEqual(plan["combine"], "all")
        self.assertEqual(plan["state"], "DRAFT")
        volume = next(c for c in plan["conditions"] if c["feature"] == "rel_volume_20d")
        self.assertEqual(volume["value"], 1.5)
        self.assertEqual(volume["unit"], "multiple")
        self.assertIn("未使用期間", render_plans(plans))
        self.assertFalse(plan["automatic_adoption"])

    def test_unknown_units_free_text_and_redundant_conditions_rejected(self):
        for content in (self.content(["turnover_is_volume"]), self.content(explanation="中央値を観測値と主張"),
                        self.content(["volume_ge_1_5", "volume_ge_2"]),
                        '{"hypotheses":[],"hypotheses":[]}', '{"hypotheses":NaN}'):
            with self.subTest(content=content):
                with self.assertRaises(ValueError):
                    validate_plans(content, "study-test", "synthetic")

    def test_mismatched_reason_and_duplicate_plans_rejected(self):
        with self.assertRaisesRegex(ValueError, "reason does not match"):
            validate_plans(self.content(["ret5_le_0_05"]), "study-test", "synthetic")
        value = json.loads(self.content())
        value["hypotheses"].append(value["hypotheses"][0])
        with self.assertRaisesRegex(ValueError, "duplicated hypothesis"):
            validate_plans(json.dumps(value), "study-test", "synthetic")


if __name__ == "__main__":
    unittest.main()
