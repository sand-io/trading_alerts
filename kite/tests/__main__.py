"""Run from the repository root with python -m kite.tests [-v]."""
import sys
import unittest

from . import KITE_DIR

unittest.main(module=None, argv=[
    sys.argv[0], 'discover', '-s', str(KITE_DIR / 'tests'),
    '-t', str(KITE_DIR.parent), *sys.argv[1:],
])
