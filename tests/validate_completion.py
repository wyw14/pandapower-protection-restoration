"""Cross-check trusted pytest evidence before the outer entry point completes."""

import hashlib
import json
from pathlib import Path


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def validate_report(manifest, data, tree, pytest_status):
    """Accept complete passing or failing runs, never missing or skipped work."""
    expected = manifest["nodeids"]
    _require(
        bool(expected) and len(expected) == len(set(expected)),
        "Invalid test identities",
    )
    _require(
        type(pytest_status) is int and pytest_status in (0, 1),
        "Pytest did not finish normally",
    )
    _require(
        type(data["exitstatus"]) is int and data["exitstatus"] == pytest_status,
        "Session exit status mismatch",
    )
    _require(not data["collection_errors"], "Collection failed")
    _require(
        sorted(data["collected"]) == sorted(expected),
        "Collected identities differ from contract",
    )
    _require(
        set(data["outcomes"]) == set(expected),
        "Executed identities differ from contract",
    )
    expected_xml = {}
    failed = set()
    for nodeid in expected:
        parts = nodeid.split("::")
        _require(
            parts[0].endswith(".py") and len(parts) >= 2, "Unsupported test identity"
        )
        classname = parts[0][:-3].replace("/", ".")
        if len(parts) > 2:
            classname += "." + ".".join(parts[1:-1])
        identity = (classname, parts[-1])
        _require(identity not in expected_xml, "Ambiguous JUnit identity")
        expected_xml[identity] = nodeid
        phases = data["outcomes"][nodeid]
        _require(
            len(phases) == 3
            and phases[0] == ["setup", "passed"]
            and phases[2] == ["teardown", "passed"]
            and phases[1] in (["call", "passed"], ["call", "failed"]),
            "Incomplete or skipped phases: " + nodeid,
        )
        if phases[1][1] == "failed":
            failed.add(nodeid)
    _require(
        pytest_status == int(bool(failed)), "Test failures disagree with pytest status"
    )
    failures = [
        (item["nodeid"], item["phase"], item["outcome"]) for item in data["failures"]
    ]
    _require(
        sorted(failures) == sorted((node, "call", "failed") for node in failed),
        "Failure ledger mismatch",
    )
    root = tree.getroot()
    _require(root.tag == "testsuites", "Unexpected JUnit root")
    suites = list(root)
    _require(
        len(suites) == 1 and suites[0].tag == "testsuite", "Unexpected JUnit suites"
    )
    suite = suites[0]
    cases = suite.findall("testcase")
    _require(len(cases) == len(expected), "JUnit test count mismatch")
    seen = set()
    for case in cases:
        identity = (case.get("classname"), case.get("name"))
        _require(
            identity in expected_xml and identity not in seen,
            "Unknown or repeated JUnit case",
        )
        seen.add(identity)
        nodeid = expected_xml[identity]
        _require(
            not case.findall("error") and not case.findall("skipped"),
            "JUnit case was not completed",
        )
        _require(
            len(case.findall("failure")) == int(nodeid in failed),
            "JUnit outcome disagrees with ledger: " + nodeid,
        )
    for field, value in (
        ("tests", len(expected)),
        ("failures", len(failed)),
        ("errors", 0),
        ("skipped", 0),
    ):
        _require(
            int(suite.get(field, "-1")) == value, "JUnit summary mismatch: " + field
        )
    return {
        "completion_state": "COMPLETE",
        "collection_complete": True,
        "expected_tests": len(expected),
        "collected_tests": len(data["collected"]),
        "completed_tests": len(data["outcomes"]),
        "passed_tests": len(expected) - len(failed),
        "failed_phases": len(failed),
        "warnings_count": len(data["warnings"]),
        "reward": int(not failed),
    }


def _read(path):
    _require(
        not path.is_symlink() and path.is_file(),
        "Missing or unsafe report: " + str(path),
    )
    _require(
        0 < path.stat().st_size <= 10_000_000, "Empty or oversized report: " + str(path)
    )
    return path.read_bytes()


def validate_seal(manifest_path: Path, logs: Path, tree):
    """Authenticate sources, compare independent reports and confirm published reward."""
    manifest_bytes = _read(manifest_path)
    manifest = json.loads(manifest_bytes)
    for relative, expected_digest in manifest["test_files_sha256"].items():
        path = (manifest_path.parent / relative).resolve()
        _require(
            path.is_relative_to(manifest_path.parent.resolve()), "Escaping source path"
        )
        _require(
            hashlib.sha256(_read(path)).hexdigest() == expected_digest,
            "Trusted source changed: " + relative,
        )
    data_bytes = _read(logs / "outcomes.json")
    grading_bytes = _read(logs / "grading.json")
    data = json.loads(data_bytes)
    grading = json.loads(grading_bytes)
    _require(grading.get("completion_state") == "COMPLETE", "Grader did not complete")
    _require(grading.get("timed_out") is False, "Timed out or missing timeout status")
    for field in ("grader_error", "cleanup_error", "finalization_error"):
        _require(not grading.get(field), "Grader exception: " + field)
    result = validate_report(manifest, data, tree, grading["pytest_exit_code"])
    for field in (
        "expected_tests",
        "collected_tests",
        "completed_tests",
        "passed_tests",
        "reward",
    ):
        _require(
            type(grading[field]) is int and grading[field] == result[field],
            "Grading summary mismatch: " + field,
        )
    for field in ("failed_phases", "warnings_count"):
        if field in grading:
            _require(
                grading[field] == result[field], "Grading summary mismatch: " + field
            )
    if "collection_complete" in grading:
        _require(grading["collection_complete"] is True, "Collection not complete")
    contract_hash = hashlib.sha256(manifest_bytes).hexdigest()
    _require(
        grading["test_contract_sha256"] == contract_hash, "Grading contract mismatch"
    )
    _require(
        _read(logs / "reward.txt") == (str(result["reward"]) + "\n").encode("ascii"),
        "Published reward disagrees with reports",
    )
    result["test_contract_sha256"] = contract_hash
    result["outcomes_sha256"] = hashlib.sha256(data_bytes).hexdigest()
    result["grading_sha256"] = hashlib.sha256(grading_bytes).hexdigest()
    return result
