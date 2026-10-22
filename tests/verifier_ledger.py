"""Trusted pytest ledger; installed only in the separate verifier interpreter."""
import json
import os
from pathlib import Path

COLLECTED = []
OUTCOMES = {}
COLLECTION_ERRORS = []
FAILURES = []
WARNINGS = []


def pytest_collection_finish(session):
    COLLECTED.extend(item.nodeid for item in session.items)


def pytest_collectreport(report):
    if report.failed:
        COLLECTION_ERRORS.append({"nodeid": report.nodeid, "error": str(report.longrepr)[:20000]})


def pytest_runtest_logreport(report):
    OUTCOMES.setdefault(report.nodeid, []).append([report.when, report.outcome])
    if report.failed or report.skipped:
        FAILURES.append({"nodeid": report.nodeid, "phase": report.when,
                         "outcome": report.outcome, "detail": str(report.longrepr)[:20000]})


def pytest_warning_recorded(warning_message, when, nodeid, location):
    WARNINGS.append({"nodeid": nodeid, "when": when,
                     "category": warning_message.category.__name__,
                     "message": str(warning_message.message)[:2000]})


def pytest_sessionfinish(session, exitstatus):
    target = Path(os.environ["TRUSTED_OUTCOMES_PATH"])
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps({
        "collected": COLLECTED, "outcomes": OUTCOMES,
        "collection_errors": COLLECTION_ERRORS, "failures": FAILURES,
        "warnings": WARNINGS, "exitstatus": int(exitstatus),
    }, indent=2), encoding="utf-8")
    os.replace(temporary, target)
