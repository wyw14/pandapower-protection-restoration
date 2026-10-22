#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
chmod 700 /logs/verifier
printf '0\n' > /logs/verifier/reward.txt
chmod 600 /logs/verifier/reward.txt
trap 'status=$?; if [ "$status" -ne 0 ]; then printf "0\n" > /logs/verifier/reward.txt; printf "Verifier bootstrap failed (exit %s); see install.log and test-stdout.txt.\n" "$status" > /logs/verifier/bootstrap-error.txt; fi' EXIT
(cd /tests/wheels && sha256sum -c /tests/wheels-sha256.txt) > /logs/verifier/install.log 2>&1
/usr/local/bin/python -I -m pip install --no-index --find-links=/tests/wheels --no-deps \
    -r /tests/requirements.txt >> /logs/verifier/install.log 2>&1
# An explicit pytest module entry point keeps CLI and plugin collection visible.
# This trusted plugin is installed in the verifier, never in the Agent image.
plugin_dir=$(/usr/local/bin/python -I -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
install -m 644 /tests/verifier_ledger.py "$plugin_dir/_harbor_energy_ledger.py"
# -I rejects PYTHONPATH, user-site packages and cwd import poisoning.
exec /usr/local/bin/python -I /tests/grade.py /usr/local/bin/python -I -m pytest
