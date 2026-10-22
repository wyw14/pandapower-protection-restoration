"""Only trusted test code runs here. The candidate is reached through RPC."""
import json
import os
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD'] = '1'
import pytest

class Ledger:
    def __init__(self, output):
        self.output = Path(output)
        self.collected = []
        self.outcomes = {}
        self.collection_errors = []

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_errors.append(report.nodeid)

    def pytest_runtest_logreport(self, report):
        self.outcomes.setdefault(report.nodeid, []).append([report.when, report.outcome])

    def pytest_sessionfinish(self, session, exitstatus):
        self.output.write_text(json.dumps({'collected': self.collected,
            'outcomes': self.outcomes, 'collection_errors': self.collection_errors,
            'exitstatus': int(exitstatus)}, indent=2), encoding='utf-8')

if __name__ == '__main__':
    output = sys.argv[1]
    args = [str(HERE), '-q', '-p', 'no:cacheprovider', '--tb=short',
            '-c', str(HERE / 'pytest.ini'), '--confcutdir=' + str(HERE), '--rootdir=' + str(HERE)]
    if len(sys.argv) > 2 and sys.argv[2] == '--collect-only':
        args.append('--collect-only')
    result = pytest.main(args, plugins=[Ledger(output)])
    sys.exit(int(result))
