"""Root-owned verdict from exact trusted test identities and all three phases."""

import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import stat
import subprocess
import sys
import sysconfig
import tempfile
import time

HERE = Path(__file__).resolve().parent
LOGS = Path("/logs/verifier")
LOGS.mkdir(parents=True, exist_ok=True)
LOGS.chmod(0o700)
START = time.monotonic()
process = None
private = Path(tempfile.mkdtemp(prefix="trusted-verifier-"))
private.chmod(0o700)
pids = private / "candidate-pids.txt"


def reward(value):
    fd, name = tempfile.mkstemp(prefix=".reward-", dir=LOGS)
    with os.fdopen(fd, "w", encoding="ascii") as output:
        output.write(str(value) + "\n")
    os.chmod(name, 0o600)
    target = LOGS / "reward.txt"
    # Docker Desktop maps mode 0444 to the Windows read-only attribute, which
    # prevents replace even for container root. Root-only 0600 plus the probed
    # isolation policy prevents candidate access without that host incompatibility.
    if (
        target.exists()
        and not target.is_symlink()
        and stat.S_ISREG(target.lstat().st_mode)
    ):
        target.chmod(0o600)
    os.replace(name, target)


reward(0)
record = {
    "verifier_version": "isolated-rpc-v2",
    "reward": 0,
    "timed_out": False,
    "verdict": "bootstrap_error",
    "stage": "manifest",
}


def terminate(signum, frame):
    raise InterruptedError("Verifier received signal " + str(signum))


for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(number, terminate)


def cleanup():
    targets = [process.pid] if process is not None else []
    if pids.exists():
        targets.extend(int(x) for x in pids.read_text().splitlines() if x.isdigit())
    for pid in targets:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process is not None:
        process.wait(timeout=5)


def environment_check():
    details = {
        "python": platform.python_version(),
        "platform": sys.platform,
        "architecture": platform.machine(),
        "kernel": platform.release(),
        "bootstrap_uid": os.geteuid(),
        "required_policy": "uid-landlock-seccomp",
        "required_landlock_abi": 3,
    }
    try:
        if os.geteuid() != 0:
            raise RuntimeError(
                "Verifier bootstrap must run as root; candidate runs as nobody"
            )
        sys.path.insert(0, str(HERE))
        from candidate_sandbox import prepare_sandbox

        details["sandbox"] = prepare_sandbox(str(HERE), str(LOGS), str(private))
    except BaseException as exc:
        details["compatible"] = False
        details["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    else:
        details["compatible"] = True
    finally:
        (LOGS / "environment.json").write_text(
            json.dumps(details, indent=2), encoding="utf-8"
        )


try:
    manifest = json.loads((HERE / "test-contract.json").read_text(encoding="utf-8"))
    expected = manifest["nodeids"]
    if not expected or len(expected) != len(set(expected)):
        raise RuntimeError("Invalid expected test identity manifest")
    record["expected_tests"] = len(expected)
    record["test_contract_sha256"] = hashlib.sha256(
        (HERE / "test-contract.json").read_bytes()
    ).hexdigest()
    for name, digest in manifest["test_files_sha256"].items():
        if hashlib.sha256((HERE / name).read_bytes()).hexdigest() != digest:
            raise RuntimeError("Test source differs from collected contract: " + name)
    plugin = Path(sysconfig.get_path("purelib")) / "_harbor_energy_ledger.py"
    if (
        hashlib.sha256(plugin.read_bytes()).hexdigest()
        != manifest["test_files_sha256"]["verifier_ledger.py"]
    ):
        raise RuntimeError("Installed trusted pytest ledger differs from contract")
    record["stage"] = "environment"
    record["verdict"] = "environment_error"
    environment_check()
    report = private / "outcomes.json"
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(private),
        "MPLCONFIGDIR": str(private / "mpl"),
        "MPLBACKEND": "Agg",
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "CANDIDATE_PID_LEDGER": str(pids),
        "TRUSTED_OUTCOMES_PATH": str(report),
    }
    entry = sys.argv[1:] or [sys.executable, "-I", "-m", "pytest"]
    if entry != [sys.executable, "-I", "-m", "pytest"]:
        raise RuntimeError("Expected the isolated python -m pytest entry point")
    command = entry + [
        str(HERE / "test_trajectory.py"),
        "-q",
        "-p",
        "_harbor_energy_ledger",
        "-p",
        "no:cacheprovider",
        "--tb=short",
        "-c",
        str(HERE / "pytest.ini"),
        "--confcutdir=" + str(HERE),
        "--rootdir=" + str(HERE),
        "--junitxml=" + str(LOGS / "junit.xml"),
    ]
    record["pytest_command"] = command
    record["stage"] = "tests"
    record["verdict"] = "test_failure"
    with open(LOGS / "pytest.log", "wb") as output:
        process = subprocess.Popen(
            command,
            stdout=output,
            stderr=subprocess.STDOUT,
            cwd="/",
            env=env,
            start_new_session=True,
        )
        try:
            process.wait(timeout=270)
        except subprocess.TimeoutExpired:
            record["timed_out"] = True
            cleanup()
    record["pytest_exit_code"] = process.returncode
    if report.is_file() and report.stat().st_size <= 10_000_000:
        data = json.loads(report.read_text(encoding="utf-8"))
        (LOGS / "outcomes.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8"
        )
        collected, outcomes = data["collected"], data["outcomes"]
        record["collected_tests"] = len(collected)
        record["completed_tests"] = len(outcomes)
        record["failed_phases"] = len(data["failures"])
        record["warnings_count"] = len(data["warnings"])
        record["passed_tests"] = sum(
            phases == [["setup", "passed"], ["call", "passed"], ["teardown", "passed"]]
            for phases in outcomes.values()
        )
        valid = (
            not record["timed_out"]
            and process.returncode == 0
            and data["exitstatus"] == 0
            and not data["collection_errors"]
            and sorted(collected) == sorted(expected)
            and set(outcomes) == set(expected)
            and record["passed_tests"] == len(expected)
        )
        if valid:
            record["reward"] = 1
            record["verdict"] = "passed"
        record["stage"] = "complete"
    else:
        record["verdict"] = "timeout" if record["timed_out"] else "report_error"
        record["grader_error"] = "Trusted test report absent or oversized"
except BaseException as exc:
    record["reward"] = 0
    record["grader_error"] = type(exc).__name__ + ": " + str(exc)
finally:
    try:
        cleanup()
    except BaseException as exc:
        record["reward"] = 0
        record["verdict"] = "cleanup_error"
        record["cleanup_error"] = type(exc).__name__ + ": " + str(exc)
    record["elapsed_seconds"] = time.monotonic() - START
    try:
        # Evidence and output must succeed before the final success commit.
        # In particular, a failed diagnostic write must never leave reward=1.
        (LOGS / "grading.json").write_text(
            json.dumps(record, indent=2), encoding="utf-8"
        )
        print(json.dumps(record), flush=True)
        reward(record["reward"])
    except BaseException as exc:
        record["reward"] = 0
        record["verdict"] = "finalization_error"
        record["finalization_error"] = type(exc).__name__ + ": " + str(exc)
        try:
            (LOGS / "grading.json").write_text(
                json.dumps(record, indent=2), encoding="utf-8"
            )
        finally:
            reward(0)
        raise SystemExit(2)
