#!/bin/bash
set -euo pipefail
umask 077
reward=/logs/verifier/reward.txt
JUNIT_REPORT=/logs/verifier/junit.xml
workspace=
active_pgid=
watchdog_pgid=
stage=bootstrap
complete=0

reset_reward() {
    local temporary unsafe=0
    if [[ "$EUID" != 0 || -L /logs || -L /logs/verifier ]]; then
        printf '%s\n' 'Unsafe verifier directory or non-root bootstrap' >&2
        return 1
    fi
    mkdir -p /logs/verifier || return 1
    chown 0:0 /logs /logs/verifier || return 1
    chmod 0700 /logs /logs/verifier || return 1
    # Harbor prefers JSON over text; this invocation owns both output names.
    local legacy=/logs/verifier/reward.json
    if [[ -L "$legacy" ]]; then
        unsafe=1
    elif [[ -e "$legacy" && ! -f "$legacy" ]]; then
        printf '%s\n' 'Legacy reward must be a regular file' >&2
        return 1
    elif [[ -f "$legacy" ]]; then
        if [[ "$(stat -c %h -- "$legacy")" != 1 ]]; then
            unsafe=1
        else
            chmod 0600 "$legacy" || return 1
        fi
    fi
    rm -f -- "$legacy" || return 1
    if [[ -L "$reward" ]]; then
        # Replace the link itself without following or chmod-ing its target;
        # the current run still fails because its initial output was unsafe.
        unsafe=1
    elif [[ -e "$reward" && ! -f "$reward" ]]; then
        printf '%s\n' 'Reward must be a regular file' >&2
        return 1
    fi
    temporary="$(mktemp /logs/verifier/.reward-bootstrap-XXXXXXXX)" || return 1
    printf '0\n' > "$temporary" || {
        rm -f -- "$temporary"
        return 1
    }
    chmod 0600 "$temporary" || {
        rm -f -- "$temporary"
        return 1
    }
    # Docker Desktop may map 0444 to the Windows read-only attribute. The
    # trusted owner unlocks only a non-linked ordinary output before replace.
    if [[ "$unsafe" == 0 && -e "$reward" ]]; then
        if [[ "$(stat -c %h -- "$reward")" != 1 ]]; then
            unsafe=1
        elif ! chmod 0600 "$reward"; then
            rm -f -- "$temporary"
            return 1
        fi
    fi
    if ! mv -fT -- "$temporary" "$reward"; then
        rm -f -- "$temporary"
        return 1
    fi
    [[ "$unsafe" == 0 ]]
}

kill_tracked() {
    local ledger pid
    if [[ -n "$workspace" && -d "$workspace" ]]; then
        for ledger in "$workspace/candidate-pids.txt" "$workspace/pytest-pids.txt"; do
            if [[ -f "$ledger" && ! -L "$ledger" ]]; then
                while IFS= read -r pid; do
                    if [[ "$pid" =~ ^[1-9][0-9]*$ ]]; then
                        kill -KILL -- "-$pid" 2>/dev/null || true
                    fi
                done < "$ledger"
            fi
        done
    fi
}

stop_active() {
    if [[ -n "$active_pgid" ]]; then
        kill -TERM -- "-$active_pgid" 2>/dev/null || true
        # Give grade.py a bounded interval to publish its interrupted verdict
        # and clean the separate candidate and pytest sessions.
        for _ in {1..20}; do
            kill -0 -- "-$active_pgid" 2>/dev/null || break
            sleep 0.1
        done
        kill_tracked
        kill -KILL -- "-$active_pgid" 2>/dev/null || true
        wait "$active_pgid" 2>/dev/null || true
        active_pgid=
    else
        kill_tracked
    fi
}

finish() {
    local status=$?
    trap - EXIT HUP INT TERM
    set +e
    stop_active
    if [[ -n "$watchdog_pgid" ]]; then
        kill -KILL -- "-$watchdog_pgid" 2>/dev/null || true
        wait "$watchdog_pgid" 2>/dev/null || true
    fi
    if [[ "$status" != 0 || "$complete" != 1 ]]; then
        reset_reward || status=1
        if [[ ! -L /logs && ! -L /logs/verifier && -d /logs/verifier ]]; then
            printf '{"reward":0,"completion_state":"INCOMPLETE","verdict":"bootstrap_or_interruption_error","stage":"%s","shell_exit_code":%s}\n' \
                "$stage" "$status" > /logs/verifier/bootstrap-error.json
        fi
    fi
    if [[ -L "$reward" || ! -f "$reward" ]] || ! chmod 0444 "$reward"; then
        printf '%s\n' 'Unable to seal reward; fail closed' >&2
        reset_reward
        chmod 0444 "$reward" 2>/dev/null || true
        status=1
    fi
    exit "$status"
}

interrupt() {
    stage="signal_$1"
    complete=0
    exit "$1"
}

run_stage() {
    setsid "$@" <&0 &
    active_pgid=$!
    local status=0
    wait "$active_pgid" || status=$?
    active_pgid=
    return "$status"
}

trap finish EXIT
trap 'interrupt 129' HUP
trap 'interrupt 130' INT
trap 'interrupt 143' TERM
# This budget includes bootstrap, the unchanged 270-second scientific test
# allowance and cleanup. Harbor's 300-second limit is the outermost backstop.
setsid /bin/bash -c 'sleep 295; kill -TERM "$1"' watchdog "$$" &
watchdog_pgid=$!
reset_reward
# Only reports created by this invocation may establish completion. Refuse
# linked or non-file outputs rather than inheriting another run's evidence.
for report in "$JUNIT_REPORT" /logs/verifier/outcomes.json /logs/verifier/grading.json /logs/verifier/completion.json; do
    if [[ -L "$report" || ( -e "$report" && ! -f "$report" ) ]]; then
        printf 'INCOMPLETE: unsafe prior report: %s\n' "$report" >&2
        exit 1
    fi
    rm -f -- "$report"
done
workspace="$(mktemp -d /tmp/trusted-verifier-XXXXXXXX)"
chmod 0700 "$workspace"
export PRIVATE_VERIFIER_WORKSPACE="$workspace"
stage=install
run_stage /usr/bin/timeout --signal=TERM --kill-after=2s 15s /bin/bash -c '
    set -euo pipefail
    cd /tests/wheels
    sha256sum -c /tests/wheels-sha256.txt
    /usr/local/bin/python -B -I -m pip install --no-index --find-links=/tests/wheels --no-deps -r /tests/requirements.txt
    plugin_dir=$(/usr/local/bin/python -B -I -c "import sysconfig; print(sysconfig.get_path(\"purelib\"))")
    install -m 644 /tests/verifier_ledger.py "$plugin_dir/_harbor_energy_ledger.py"
' > /logs/verifier/install.log 2>&1
stage=grade
# Keep the shell alive so signals during bootstrap and finalization also
# force zero and remove descendants. The verifier owns its own test deadline.
run_stage /usr/local/bin/python -B -I /tests/grade.py --junitxml="$JUNIT_REPORT" /usr/local/bin/python -B -I -m pytest
stage=completion
if ! test -s "$JUNIT_REPORT"; then
    printf '%s\n' 'INCOMPLETE: current JUnit report is absent or empty' >&2
    exit 1
fi
run_stage /usr/local/bin/python -B -I - "$JUNIT_REPORT" <<'PY'
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, "/tests")
from validate_completion import validate_seal

logs = Path("/logs/verifier")
try:
    report = ET.parse(sys.argv[1])
    completion = validate_seal(Path("/tests/test-contract.json"), logs, report)
except Exception as error:
    completion = {"completion_state": "INCOMPLETE", "reward": 0,
                  "error": type(error).__name__ + ": " + str(error)}
    (logs / "completion.json").write_text(json.dumps(completion, indent=2), encoding="utf-8")
    print(json.dumps(completion), file=sys.stderr)
    raise SystemExit(1)
(logs / "completion.json").write_text(json.dumps(completion, indent=2), encoding="utf-8")
print(json.dumps(completion))
PY
complete=1
stage=complete
