"""Saved JSON strategies validated by the condition parser."""
import hashlib
import json
import math
import re
from pathlib import Path
from conditions import Condition
from nnf_strategy import build_rules, settings_from_json, WARNING
from instrument_scope import NNF_SCOPE

CONFIRMATION_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d")


class StrategyStore:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / 'strategies'
        self.selection = self.root / 'data' / 'active_strategy.json'

    def profiles(self):
        profiles = []
        if self.directory.exists():
            for path in sorted(self.directory.glob('*.json')):
                try:
                    profile = self.load(path.stem)
                    profiles.append({k: profile[k] for k in ('id', 'name')})
                except (ValueError, OSError, TypeError, KeyError) as exc:
                    profiles.append({'id': path.stem, 'name': path.stem, 'error': str(exc)})
        return profiles

    def selected(self):
        if self.selection.exists():
            selected = json.loads(self.selection.read_text())['id']
            if selected in ('nnf_4h_1h_15m', 'nnf_15m_5m_3m') and (self.directory / 'nnf.json').exists():
                return 'nnf'
            return selected
        return 'default'

    def stock_alert_strategy(self):
        if not self.selection.exists():
            return 'default'
        saved = json.loads(self.selection.read_text())
        identifier = saved.get('stock_alert_strategy', saved.get('id', 'default'))
        return 'default' if identifier in ('nnf', 'nnf_4h_1h_15m', 'nnf_15m_5m_3m') else identifier

    def save_selection(self, strategy_id, stock_alert_strategy=None):
        self.selection.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.selection.with_suffix('.tmp')
        payload = {'id': strategy_id}
        if stock_alert_strategy is not None:
            payload['stock_alert_strategy'] = stock_alert_strategy
        temporary.write_text(json.dumps(payload))
        temporary.replace(self.selection)

    def load(self, strategy_id):
        if not re.fullmatch(r'[a-zA-Z0-9_-]+', strategy_id):
            raise ValueError('Invalid strategy ID')
        raw = json.loads((self.directory / f'{strategy_id}.json').read_text())
        threshold = float(raw.get('threshold', 100))
        cooldown = raw.get('cooldown_seconds')
        if not math.isfinite(threshold) or not 0 < threshold <= 100:
            raise ValueError('Threshold must be greater than 0 and at most 100')
        if cooldown is not None and (not isinstance(cooldown, (float, int)) or not math.isfinite(cooldown) or cooldown < 0):
            raise ValueError('Cooldown must be a nonnegative number')
        engine = raw.get('engine', 'conditions')
        if engine not in ('conditions', 'nnf'):
            raise ValueError('Unknown strategy engine')
        scope = raw.get('instrument_scope')
        if scope is not None and scope != NNF_SCOPE:
            raise ValueError('instrument_scope must select nearest standard MCX GOLD and CRUDEOIL futures')
        if engine == 'nnf' and scope != NNF_SCOPE:
            raise ValueError('NNF requires its MCX GOLD and CRUDEOIL instrument_scope')
        parsed = build_rules(settings_from_json(raw)) if engine == 'nnf' else {}
        for side in (() if engine == 'nnf' else ('bullish', 'bearish')):
            lines = raw.get(side, [])
            if not isinstance(lines, list) or any(not isinstance(line, str) for line in lines):
                raise ValueError(f'{side} must be a list of rule strings')
            parsed[side] = []
            for line in lines:
                if not line.strip() or line.lstrip().startswith(('#', '//')):
                    continue
                condition = Condition.parse(line.strip())
                if condition is None:
                    raise ValueError(f'Invalid {side} rule: {line}')
                if condition.interval not in {'tick', '1m', '3m', '5m', '15m', '30m', '1h', '4h', '1d'}:
                    raise ValueError(f'Unsupported interval: {condition.interval}')
                parsed[side].append(condition)
        if not any(parsed.values()):
            raise ValueError('Strategy must contain at least one rule')
        rule_intervals = {rule.interval for rules in parsed.values() for rule in rules} - {'tick'}
        inferred = ('3m' if engine == 'nnf' else
                    next(iter(rule_intervals)) if len(rule_intervals) == 1 else '5m')
        confirmation = raw.get('confirmation_timeframe', inferred)
        if confirmation not in CONFIRMATION_TIMEFRAMES:
            raise ValueError('confirmation_timeframe must be one of: ' + ', '.join(CONFIRMATION_TIMEFRAMES))
        name = raw.get('name', strategy_id)
        if not isinstance(name, str) or not name.strip():
            raise ValueError('Strategy name must be nonempty')
        warnings = [WARNING] if engine == 'nnf' else []
        if engine == 'nnf' and threshold < 100:
            warnings.append('Partial-match NNF: alerts use the configured percentage of rules; '
                            'trend bias and individual PDF filters may fail. This is not the strict PDF workflow.')
        return {'id': strategy_id, 'name': name, 'threshold': threshold,
                'engine': engine, 'warnings': warnings,
                'instrument_scope': scope,
                'cooldown_seconds': cooldown, 'confirmation_timeframe': confirmation, **parsed,
                'version': hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16]}
