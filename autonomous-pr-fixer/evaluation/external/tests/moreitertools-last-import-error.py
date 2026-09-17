from unittest import TestCase

import more_itertools as mi
from more_itertools.more import _last_reversed_safe


class LastTests(TestCase):
    def test_reversed_is_none(self):
        # See https://github.com/more-itertools/more-itertools/issues/1001
        class ReversedIsNone:
            __reversed__ = None

            def __iter__(self):
                return iter([1])

        self.assertEqual(mi.last(ReversedIsNone()), 1)
