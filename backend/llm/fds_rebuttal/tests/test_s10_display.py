"""Offline checks for fact-backed S10 display units and dates."""
import unittest

from S10_dialogue_generation.generate_momo_dialogue import fact_period, fact_value


class S10DisplayTests(unittest.TestCase):
    def test_rounded_korean_amounts(self):
        self.assertEqual(fact_value({"value": 104135755017, "unit": "KRW"}), "약 1,041억 원")
        self.assertEqual(fact_value({"value": 156593558827, "unit": "KRW"}), "약 1,566억 원")

    def test_exact_and_large_amounts(self):
        self.assertEqual(fact_value({"value": 100000000, "unit": "KRW"}), "1억 원")
        self.assertEqual(fact_value({"value": 1200000000000, "unit": "KRW"}), "1조 2,000억 원")

    def test_quarter_end_and_material_date(self):
        self.assertEqual(fact_period({"period_end": "2025-09-30", "frequency": "quarterly"}), "2025년 3분기")
        self.assertEqual(fact_period({"period_end": "2025-09-29", "frequency": "quarterly"}), "2025-09-29 기준")


if __name__ == "__main__":
    unittest.main()
