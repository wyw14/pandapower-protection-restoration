"""Untrusted execution side. It never loads pytest, tests or expected results."""

import importlib.util
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from wire_codec import pack, unpack
from candidate_sandbox import restrict

MAX_WIRE = 16 * 1024 * 1024

# Capture actual built-in classes before importing untrusted candidate modules.
# The protocol carries supported exception memberships, not a candidate class name.
_EXCEPTION_TYPE = type
_EX_IS_SUBCLASS = issubclass
_ENUMERATE = enumerate
_EXCEPTION_FAMILIES = (
    (NotImplementedError, "NotImplementedError"),
    (ValueError, "ValueError"),
    (TypeError, "TypeError"),
    (RuntimeError, "RuntimeError"),
)


def exception_response(exc):
    """Preserve built-in exception families across the isolated RPC boundary."""
    actual_type = _EXCEPTION_TYPE(exc)
    family = None
    family_mask = 0
    for index, (base, name) in _ENUMERATE(_EXCEPTION_FAMILIES):
        if _EX_IS_SUBCLASS(actual_type, base):
            family_mask |= 1 << index
            if family is None:
                family = name
    return {
        "ok": False,
        "exception": family,
        "exception_mask": family_mask,
        "exception_type": actual_type.__name__,
        "message": str(exc)[:2000],
    }


def main():
    """Apply mandatory isolation before loading or executing candidate code."""
    source, scratch, inputs, mode = sys.argv[1:]
    response = os.fdopen(os.dup(1), "w", buffering=1)
    # Candidate print() / os.write(1, ...) cannot become protocol messages.
    os.dup2(2, 1)
    policy = restrict(source, scratch, inputs)
    if mode == "cli":
        raw = sys.stdin.buffer.read(MAX_WIRE + 1)
        if len(raw) > MAX_WIRE:
            raise ValueError("CLI input too large")
        # exec preserves the filesystem and syscall policies and drops bootstrap.
        os.dup2(response.fileno(), 1)
        # Parent feeds the actual CLI input directly in this mode; no wrapper JSON.
        input_path = Path(scratch) / "stdin.json"
        input_path.write_bytes(raw)
        fd = os.open(input_path, os.O_RDONLY)
        os.dup2(fd, 0)
        os.close(fd)
        bootstrap = (
            "import runpy,sys;sys.path.insert(0,sys.argv[1]);"
            'runpy.run_path(sys.argv[1]+"/candidate.py",run_name="__main__")'
        )
        os.execv(sys.executable, [sys.executable, "-B", "-I", "-c", bootstrap, source])
    sys.path.insert(0, source)
    spec = importlib.util.spec_from_file_location(
        "candidate", Path(source) / "candidate.py"
    )
    candidate = importlib.util.module_from_spec(spec)
    sys.modules["candidate"] = candidate
    spec.loader.exec_module(candidate)
    response.write(
        json.dumps(
            {
                "ready": True,
                "landlock_abi": policy["landlock"]["abi"],
                "sandbox_policy": policy,
            }
        )
        + "\n"
    )
    for line in sys.stdin:
        if len(line) > MAX_WIRE:
            raise ValueError("RPC input too large")
        request = json.loads(line)
        args, kwargs = unpack(request["args"]), unpack(request["kwargs"])
        try:
            value = getattr(candidate, request["function"])(*args, **kwargs)
            message = {"ok": True, "value": pack(value)}
        except Exception as exc:
            message = exception_response(exc)
        message["args_after"] = pack(args)
        message["kwargs_after"] = pack(kwargs)
        response.write(
            json.dumps(message, allow_nan=False, separators=(",", ":")) + "\n"
        )


if __name__ == "__main__":
    main()
