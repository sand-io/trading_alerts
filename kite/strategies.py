"""Saved JSON strategies validated by the condition parser."""
import hashlib
import json
import math
import re
from pathlib import Path
from conditions import Condition


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
            return json.loads(self.selection.read_text())['id']
        return 'default'

    def save_selection(self, strategy_id):
        self.selection.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.selection.with_suffix('.tmp')
        temporary.write_text(json.dumps({'id': strategy_id}))
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
        parsed = {}
        for side in ('bullish', 'bearish'):
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
        name = raw.get('name', strategy_id)
        if not isinstance(name, str) or not name.strip():
            raise ValueError('Strategy name must be nonempty')
        return {'id': strategy_id, 'name': name, 'threshold': threshold,
                'cooldown_seconds': cooldown, **parsed,
                'version': hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16]}
