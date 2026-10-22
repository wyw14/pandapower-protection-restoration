"""JSON service entry point for the line-temperature operations study."""
import json
import sys
from thermal_trajectory import analyze


def main():
    try:
        answer = analyze(json.load(sys.stdin))
        print(json.dumps(answer, allow_nan=False))
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
