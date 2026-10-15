"""Strategy instrument eligibility and standard commodity future resolution."""
import re
from datetime import date, datetime

NNF_SCOPE = {'exchange': 'MCX', 'underlyings': ['GOLD', 'CRUDEOIL'],
             'instrument_type': 'FUT', 'expiry_policy': 'nearest_unexpired'}
STANDARD_FUTURE = re.compile(r'^(GOLD|CRUDEOIL)\d{2}[A-Z]{3}FUT$')


def underlying(symbol):
    match = STANDARD_FUTURE.fullmatch(symbol.split(':')[-1])
    return match.group(1) if match else None


def applies(profile, data, configured_symbols=None):
    scope = profile.get('instrument_scope')
    if scope:
        return (getattr(data, 'exchange', data.symbol.split(':')[0]) == scope['exchange']
                and underlying(data.symbol) in scope['underlyings']
                and getattr(data, 'instrument_type', 'FUT') == scope['instrument_type'])
    # Supplemental commodity subscriptions must not expand existing profiles.
    if configured_symbols is not None:
        return data.symbol in configured_symbols
    return getattr(data, 'exchange', data.symbol.split(':')[0]) != 'MCX'


def nearest_futures(instruments, today):
    selected = {}
    for instrument in instruments:
        base = underlying(instrument.get('tradingsymbol', ''))
        if (instrument.get('exchange') != 'MCX' or
                instrument.get('instrument_type') != 'FUT' or base is None):
            continue
        expiry = instrument.get('expiry')
        try:
            expiry = expiry.date() if isinstance(expiry, datetime) else (
                expiry if isinstance(expiry, date) else date.fromisoformat(str(expiry)))
        except ValueError:
            continue
        if expiry < today or not instrument.get('instrument_token'):
            continue
        if base not in selected or expiry < selected[base][0]:
            selected[base] = (expiry, instrument)
    return {base: item[1] for base, item in selected.items()}
