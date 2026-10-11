#!/bin/sh
# Recovery session only. No installed-system changes; no shell startup files.
BASE=${1:?server URL required}
umask 077
for tool in curl sh mktemp date sleep ps awk kill cat mkdir mv rm df touch; do
    command -v "$tool" >/dev/null 2>&1 || { echo "Missing required tool: $tool" >&2; exit 1; }
done
net() { curl --noproxy '*' -fsS --connect-timeout 2 --max-time 5 "$@"; }
TEMP_ROOT=${BRIDGE_TMPDIR:-/tmp}
if [ "${BRIDGE_LOCAL_TEST:-0}" != 1 ]; then
    # Gather into shell memory, then send directly. No diagnostic files on an
    # unverified Data mount. The operator selects storage after checking mounts.
    report=$({
        echo "Recovery bridge preflight pid=$$"
        date -u
        uname -a
        command -v sw_vers >/dev/null 2>&1 && sw_vers
        echo '--- temporary path resolution ---'
        for candidate in /tmp /private/tmp /private/var/tmp; do
            echo "$candidate"
            (cd "$candidate" && pwd -P)
        done
        echo '--- filesystem usage ---'
        df -P / /tmp /private/var/tmp /System/Volumes/Data
        echo '--- mount table ---'
        command -v mount >/dev/null 2>&1 && mount
        if command -v diskutil >/dev/null 2>&1; then
            echo '--- diskutil info temporary filesystem ---'
            diskutil info /tmp
            echo '--- diskutil info root ---'
            diskutil info /
            echo '--- diskutil list ---'
            diskutil list
        fi
    } 2>&1)
    until printf '%s\n' "$report" | net --data-binary @- "$BASE/preflight" >/dev/null; do
        sleep 2
    done
    echo 'Mount report sent. Waiting for verified temporary storage...'
    while :; do
        if TEMP_ROOT=$(net "$BASE/preflight"); then
            case "$TEMP_ROOT" in WAIT) ;; STOP) exit 0;; /*) break;;
                *) echo 'Invalid temporary-storage response.' >&2; exit 1;; esac
        fi
        sleep 2
    done
fi
TEMP_ROOT=$(cd "$TEMP_ROOT" && pwd -P) || exit 1
case "$TEMP_ROOT" in /tmp|/private/tmp|/private/var/tmp|/System/Volumes/Data/private/tmp|/System/Volumes/Data/private/var/tmp) ;; *)
    echo "Refusing unverified temporary location: $TEMP_ROOT" >&2; exit 1;; esac
echo "Worker temporary filesystem:"
df -P "$TEMP_ROOT" || exit 1
SPOOL=$(mktemp -d "$TEMP_ROOT/recovery-bridge.XXXXXXXX") || exit 1
WORKER=${SPOOL##*/}
export TMPDIR="$SPOOL"
export BRIDGE_ARTIFACT_DIR="$SPOOL/artifacts"
mkdir "$BRIDGE_ARTIFACT_DIR" || exit 1
echo idle > "$SPOOL/state"
JOBPID= JOBGROUP= HEARTPID= JOB=

utc() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }
retry_file() {
    while ! net --data-binary "@$2" "$BASE/$1?worker=$WORKER" > "$SPOOL/response"; do
        if [ -f "$SPOOL/stop" ]; then
            echo "Upload interrupted; result retained at $SPOOL (server status may be unknown)." >&2
            exit 0
        fi
        sleep 2
    done
}
terminate_tree() {
    [ -n "$JOBPID" ] || return
    # Snapshot descendants while their parents still exist; kill children first.
    ps -axo pid=,ppid= | awk -v root="$JOBPID" '
      { parent[$1]=$2; ids[NR]=$1 } END {
        for (i=NR; i>0; i--) { p=ids[i]; walk=p;
          for (n=0; n<100 && walk>1; n++) {
            if (walk==root) { print p; break } walk=parent[walk]
          }
        }
      }' > "$SPOOL/pids"
    for sig in TERM KILL; do
        if [ "$JOBGROUP" = yes ]; then
            kill -"$sig" -- "-$JOBPID" 2>/dev/null || :
        fi
        while read -r p; do kill -"$sig" "$p" 2>/dev/null || :; done < "$SPOOL/pids"
        [ "$sig" = TERM ] && sleep 2
    done
}
cleanup() {
    trap '' INT TERM HUP
    [ -n "$JOBPID" ] && terminate_tree
    [ -n "$HEARTPID" ] && kill "$HEARTPID" 2>/dev/null
    # Keep temporary spool on interruption for inspection. Reboot removes it.
    echo "Worker stopped. Temporary spool: $SPOOL"
}
trap 'touch "$SPOOL/stop"' INT TERM HUP
trap cleanup EXIT

# Independent heartbeat remains alive during execution, upload retries, and hangs.
(
    trap 'exit 0' INT TERM HUP
    while :; do
        state=$(cat "$SPOOL/state")
        if net -X POST "$BASE/heartbeat?worker=$WORKER&state=$state" > "$SPOOL/heartbeat"; then
            case "$(cat "$SPOOL/heartbeat")" in STOP) touch "$SPOOL/stop";; esac
        fi
        sleep 2
    done
) &
HEARTPID=$!

while [ ! -f "$SPOOL/stop" ]; do
    echo idle > "$SPOOL/state"
    if ! net "$BASE/next?worker=$WORKER" > "$SPOOL/next"; then sleep 2; continue; fi
    read -r JOB LIMIT < "$SPOOL/next"
    case "$JOB" in WAIT) sleep 2; continue;; STOP) break;; esac
    case "$JOB" in *[!0-9a-f]*|'') echo 'Invalid job response; stopping.' >&2; exit 1;; esac
    case "$LIMIT" in *[!0-9]*|'') echo 'Invalid timeout; stopping.' >&2; exit 1;; esac
    echo "$JOB" > "$SPOOL/state"
    mkdir "$SPOOL/$JOB" || exit 1
    JDIR="$SPOOL/$JOB"
    # Download failures never trigger a fresh dispatch or execution attempt.
    while ! net "$BASE/job/$JOB/script.sh" -o "$JDIR/script.partial"; do
        [ -f "$SPOOL/stop" ] && exit 0
        sleep 2
    done
    mv "$JDIR/script.partial" "$JDIR/script.sh"
    utc > "$JDIR/start"
    retry_file "job/$JOB/start" "$JDIR/start"
    [ -f "$SPOOL/stop" ] && exit 0
    : > "$JDIR/stdout"
    : > "$JDIR/stderr"
    rm -f "$SPOOL/done" "$SPOOL/rc"
    # The wrapper reports completion through files; main loop never blocks in wait.
    # macOS sh supports monitor mode. Verify the group rather than assuming it.
    set -m 2>/dev/null || :
    (
        trap - EXIT INT TERM HUP
        /bin/sh "$JDIR/script.sh" > "$JDIR/stdout" 2> "$JDIR/stderr"
        echo "$?" > "$SPOOL/rc"
        touch "$SPOOL/done"
    ) &
    JOBPID=$!
    JOBGROUP=no
    group=$(ps -o pgid= -p "$JOBPID" | awk '{print $1}')
    [ "$group" = "$JOBPID" ] && JOBGROUP=yes
    set +m 2>/dev/null || :
    started=$(date +%s)
    outcome=normal
    while [ ! -f "$SPOOL/done" ]; do
        if [ -f "$SPOOL/stop" ]; then outcome=interrupted; break; fi
        now=$(date +%s)
        if [ "$((now - started))" -ge "$LIMIT" ]; then outcome=timeout; break; fi
        sleep 1
    done
    if [ "$outcome" != normal ]; then
        terminate_tree
        if [ "$outcome" = timeout ]; then rc=124; else rc=130; fi
    else
        rc=$(cat "$SPOOL/rc")
        wait "$JOBPID" 2>/dev/null || :
        if [ "$JOBGROUP" = yes ]; then
            # Reap children left in the execution group even after parent exit.
            kill -TERM -- "-$JOBPID" 2>/dev/null || :
            kill -KILL -- "-$JOBPID" 2>/dev/null || :
        fi
    fi
    JOBPID=
    { utc; echo "$rc"; echo "$outcome"; } > "$JDIR/finish"
    retry_file "job/$JOB/stdout" "$JDIR/stdout"
    retry_file "job/$JOB/stderr" "$JDIR/stderr"
    for artifact in "$BRIDGE_ARTIFACT_DIR"/*; do
        [ -f "$artifact" ] || continue
        name=${artifact##*/}
        case "$name" in *[!A-Za-z0-9_.-]*) echo "Invalid artifact name: $name" >&2; continue;; esac
        retry_file "job/$JOB/artifact/$name" "$artifact"
        rm -f "$artifact"
    done
    retry_file "job/$JOB/finish" "$JDIR/finish"
    # Outputs are now durable on the working computer.
    rm -rf "$JDIR"
done
echo stopped > "$SPOOL/state"
net -X POST "$BASE/heartbeat?worker=$WORKER&state=stopped" >/dev/null 2>&1 || :
