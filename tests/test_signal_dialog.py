"""Form validation must reject misleading or unusable timing constraints."""
import unittest

from ui.signal_plan_dialog import parse_parameters


class SignalFormTests(unittest.TestCase):
    def values(self):
        return {"green_min": "15", "green_max": "60", "yellow_min": "3", "yellow_max": "5",
                "red_min": "10", "red_max": "90", "all_red_min": "1", "all_red_max": "3",
                "cycle": "", "generations": "3", "population": "8", "seed": "42"}

    def test_empty_cycle_preserves_source_and_red_is_independent(self):
        values = self.values()
        values["red_min"], values["all_red_min"] = "12", "0"
        parsed = parse_parameters(values)
        self.assertIsNone(parsed["cycle"])
        self.assertEqual(parsed["red_min"], 12)
        self.assertEqual(parsed["all_red_min"], 0)

    def test_decimal_seconds_and_cycle(self):
        values = self.values()
        values["yellow_min"], values["cycle"] = "3,5", "88.5"
        parsed = parse_parameters(values)
        self.assertEqual(parsed["yellow_min"], 3.5)
        self.assertEqual(parsed["cycle"], 88.5)

    def test_nonfinite_and_reversed_bounds_rejected(self):
        for key, value in [("red_max", "nan"), ("green_min", "inf"),
                           ("yellow_min", "0"), ("all_red_min", "-1"),
                           ("green_max", "14"), ("cycle", "inf"), ("cycle", "0")]:
            with self.subTest(key=key, value=value):
                values = self.values()
                values[key] = value
                with self.assertRaises(ValueError):
                    parse_parameters(values)

    def test_algorithm_counts_must_be_valid_integers(self):
        for key, value in [("generations", "0"), ("population", "1"),
                           ("seed", "-1"), ("population", "3.5")]:
            with self.subTest(key=key, value=value):
                values = self.values()
                values[key] = value
                with self.assertRaises(ValueError):
                    parse_parameters(values)


if __name__ == "__main__":
    unittest.main()
