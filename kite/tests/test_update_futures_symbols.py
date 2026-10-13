import unittest
from datetime import date

from update_futures_symbols import resolve_current_futures


class TestFuturesResolver(unittest.TestCase):
    def test_uses_nearest_expiry_aliases_and_reports_missing(self):
        rows = [
            {'name': 'RELIANCE', 'tradingsymbol': 'RELIANCE26OCTFUT',
             'expiry': date(2026, 10, 27), 'instrument_type': 'FUT'},
            {'name': 'GMRAIRPORT', 'tradingsymbol': 'GMRAIRPORT26OCTFUT',
             'expiry': date(2026, 10, 27), 'instrument_type': 'FUT'},
            {'name': 'RELIANCE', 'tradingsymbol': 'RELIANCE26NOVFUT',
             'expiry': date(2026, 11, 24), 'instrument_type': 'FUT'},
        ]
        expiry, symbols, missing = resolve_current_futures(
            rows, ['RELIANCE', 'GMRINFRA', 'ACC'], date(2026, 10, 8))
        self.assertEqual(expiry, date(2026, 10, 27))
        self.assertEqual(symbols, [
            'NFO:RELIANCE26OCTFUT', 'NFO:GMRAIRPORT26OCTFUT'])
        self.assertEqual(missing, ['ACC'])


if __name__ == '__main__':
    unittest.main()
