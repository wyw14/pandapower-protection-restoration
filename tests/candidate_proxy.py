"""Trusted test-side proxy: data crosses the boundary; candidate code never does."""

import atexit
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from wire_codec import pack, unpack

MAX_WIRE = 16 * 1024 * 1024
HERE = Path(__file__).resolve().parent
PROCESSES = []


def kill_all():
    for process in PROCESSES:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


atexit.register(kill_all)


class Candidate:
    def __init__(self):
        self.process = None
        self.pending = b""
        self.folder = Path(tempfile.mkdtemp(prefix="candidate-"))
        self.folder.chmod(0o755)
        self.scratch = self.folder / "scratch"
        self.scratch.mkdir(mode=0o700)
        os.chown(self.scratch, 65534, 65534)
        self.inputs = self.folder / "inputs"
        self.inputs.mkdir(mode=0o755)
        self.abi = None

    def _command(self, mode):
        return [
            sys.executable,
            "-I",
            str(HERE / "candidate_worker.py"),
            "/app/src",
            str(self.scratch),
            str(self.inputs),
            mode,
        ]

    def _env(self):
        return {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": str(self.scratch),
            "TMPDIR": str(self.scratch),
            "MPLCONFIGDIR": str(self.scratch / "mpl"),
            "MPLBACKEND": "Agg",
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }

    def _track(self, process):
        PROCESSES.append(process)
        register = os.environ.get("CANDIDATE_PID_LEDGER")
        if register:
            with open(register, "a", encoding="ascii") as handle:
                handle.write(str(process.pid) + "\n")

    def _read(self, deadline):
        while b"\n" not in self.pending:
            wait = deadline - time.monotonic()
            if wait <= 0 or not select.select([self.process.stdout], [], [], wait)[0]:
                self.close()
                raise TimeoutError("Candidate deadline exceeded")
            block = os.read(self.process.stdout.fileno(), 65536)
            if not block:
                self.close()
                raise RuntimeError("Candidate exited without a complete RPC result")
            self.pending += block
            if len(self.pending) > MAX_WIRE:
                self.close()
                raise RuntimeError("Candidate output exceeds RPC limit")
        line, self.pending = self.pending.split(b"\n", 1)
        try:
            value = json.loads(line)
        except (ValueError, UnicodeError) as exc:
            self.close()
            raise RuntimeError("Invalid candidate JSON protocol") from exc
        if not isinstance(value, dict):
            self.close()
            raise RuntimeError("Invalid candidate protocol")
        return value

    def _start(self, deadline):
        if self.process is not None and self.process.poll() is not None:
            self.close()
        if self.process is not None:
            return
        # Debug output is bounded by RLIMIT_FSIZE, separate from RPC/report data.
        self.stderr = open(self.folder / "stderr.log", "wb")
        self.process = subprocess.Popen(
            self._command("rpc"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            env=self._env(),
            cwd="/",
            start_new_session=True,
        )
        self._track(self.process)
        ready = self._read(deadline)
        if ready.get("ready") is not True or type(ready.get("landlock_abi")) is not int:
            self.close()
            raise RuntimeError("Candidate sandbox did not initialize")
        self.abi = ready["landlock_abi"]

    def call(self, name, *args, timeout=60, **kwargs):
        deadline = time.monotonic() + timeout
        self._start(deadline)
        # This interface transports network_json and numeric objects. Strings
        # are never interpreted as trusted host-side file paths.
        before = {"args": pack(args), "kwargs": pack(kwargs)}
        request = dict(before, function=name)
        try:
            self.process.stdin.write(
                (json.dumps(request, allow_nan=False) + "\n").encode()
            )
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self.close()
            raise RuntimeError("Candidate request pipe closed") from exc
        answer = self._read(deadline)

        def canonical(value):
            return json.dumps(
                value, allow_nan=False, sort_keys=True, separators=(",", ":")
            )

        if canonical(answer.get("args_after")) != canonical(
            before["args"]
        ) or canonical(answer.get("kwargs_after")) != canonical(before["kwargs"]):
            raise AssertionError("Candidate modified its input arguments")
        if answer.get("ok") is False:
            kind = {
                "ValueError": ValueError,
                "TypeError": TypeError,
                "RuntimeError": RuntimeError,
                "NotImplementedError": NotImplementedError,
            }.get(answer.get("exception"), RuntimeError)
            raise kind(answer.get("message", "Candidate exception"))
        if answer.get("ok") is not True:
            raise RuntimeError("Invalid candidate response")
        return unpack(answer["value"])

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *args, **kwargs: self.call(name, *args, **kwargs)

    def close(self):
        process, self.process = self.process, None
        self.pending = b""
        self.abi = None
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        stream = getattr(self, "stderr", None)
        if stream is not None:
            stream.close()
            self.stderr = None


candidate = Candidate()


def fresh_call(name, *args, timeout=12, **kwargs):
    fresh = Candidate()
    try:
        return fresh.call(name, *args, timeout=timeout, **kwargs)
    finally:
        fresh.close()


def run_cli(input, timeout=20):
    fresh = Candidate()
    # Files, rather than PIPE buffering, also bound CLI output with RLIMIT_FSIZE.
    with open(fresh.folder / "cli.out", "w+b") as out, open(
        fresh.folder / "cli.err", "w+b"
    ) as err:
        process = subprocess.Popen(
            fresh._command("cli"),
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=err,
            env=fresh._env(),
            cwd="/",
            start_new_session=True,
        )
        fresh._track(process)
        try:
            process.communicate(input.encode("utf-8"), timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=3)
            raise TimeoutError("Candidate CLI deadline exceeded")
        out.seek(0)
        err.seek(0)
        stdout, stderr = out.read(MAX_WIRE + 1), err.read(MAX_WIRE + 1)
        if len(stdout) > MAX_WIRE or len(stderr) > MAX_WIRE:
            raise RuntimeError("Candidate CLI output exceeds limit")
        return SimpleNamespace(
            returncode=process.returncode,
            stdout=stdout.decode("utf-8"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )
