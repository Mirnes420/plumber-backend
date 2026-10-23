import os
import sys

os.environ.setdefault('DATABASE_URL', 'sqlite:///billing_test.db')

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

try:
    from main import calculate_cycle_minutes_overage
except Exception as exc:  # pragma: no cover
    raise SystemExit(f'Import failed before implementation: {exc}')

# starter plan includes 60 minutes, overage price per minute is $0.25
result = calculate_cycle_minutes_overage('starter', 180 * 60)
assert result['included_minutes'] == 60, result
assert result['used_minutes'] == 180, result
assert result['extra_minutes'] == 120, result
assert round(result['extra_cost'], 2) == 30.0, result

# pro plan includes 300 minutes, no overage
result = calculate_cycle_minutes_overage('pro', 180 * 60)
assert result['extra_minutes'] == 0, result
assert result['extra_cost'] == 0, result

print('billing tests pass')
