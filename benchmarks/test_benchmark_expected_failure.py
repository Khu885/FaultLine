"""Reference benchmark suite: intentionally failing until the benchmark is implemented."""

import unittest


class BenchmarkBaselineTests(unittest.TestCase):
    def test_baseline_is_not_a_product_test(self):
        self.fail("intentional benchmark baseline failure; do not run from make test")
