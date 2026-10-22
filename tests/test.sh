#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
chmod 700 /logs/verifier
printf '0\n' > /logs/verifier/reward.txt
chmod 600 /logs/verifier/reward.txt
(cd /tests/wheels && sha256sum -c /tests/wheels-sha256.txt) > /logs/verifier/install.log 2>&1
/usr/local/bin/python -I -m pip install --no-index --find-links=/tests/wheels --no-deps \
    -r /tests/requirements.txt >> /logs/verifier/install.log 2>&1
# -I rejects PYTHONPATH, user-site packages and cwd import poisoning.
exec /usr/local/bin/python -I /tests/grade.py
