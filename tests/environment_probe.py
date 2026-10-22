"""Exercise the real isolation policy without importing any candidate module."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from candidate_sandbox import restrict

source, scratch, inputs = sys.argv[1:]
abi = restrict(source, scratch, inputs)
print(json.dumps({"landlock_abi": abi, "candidate_uid": __import__("os").geteuid(),
                  "isolation_initialized": True}))
