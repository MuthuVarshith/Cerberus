from unittest import TestCase

import more_itertools as mi


class LastTests(TestCase):
    def test_reversed_is_none(self):
        # See https://github.com/more-itertools/more-itertools/issues/1001
        self.assertEqual(mi.last([1, 2, 3]), 3)
