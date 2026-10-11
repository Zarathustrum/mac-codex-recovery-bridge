# mac-codex-recovery-bridge (mcrb)

Codex runs on a working Mac. A Python HTTP server holds commands and results; a shell worker in the other Mac's Recovery environment polls it with `curl`. Recovery needs no Python, SSH server or installed agent. The working Mac needs Python 3.9+; the server uses only its standard library.

Both processes are started manually for one investigation. The bridge is unauthenticated and intended for your trusted network; stop it when finished. There is no installed service or automatic repair.

Anyone who can reach the listening port can submit commands with the worker's privileges, normally root in Recovery. Use a network you control and do not expose the server to the internet.

## Start on the working Mac

Clone or download this repository and open Terminal in the checkout. Choose the working Mac's reachable LAN address and a new persistent session directory. Keep this Mac awake; run the server in its own Terminal.

```sh
MCRB_REPO="$(pwd -P)"
MCRB_STATE="$MCRB_REPO/sessions/$(date +%Y%m%d-%H%M%S)"
MCRB_URL="http://<working-Mac-LAN-IP>:8765"
python3 "$MCRB_REPO/src/bridge.py" --state "$MCRB_STATE" \
  serve --port 8765 --public-url "$MCRB_URL"
```

The server listens on all interfaces by default. Sessions are ignored by Git and remain on the working Mac. Without `--state`, storage defaults to `sessions/default` in the repository. Reuse a directory only when intentionally resuming an investigation: server restart marks dispatched, unconfirmed jobs `unknown`; they block further dispatch and are never automatically rerun.

## Connect from Recovery

Before the target Mac needs Recovery, stage the self-contained worker in normal macOS:

```sh
install -m 700 "$MCRB_REPO/src/worker.sh" \
  /Users/Shared/mcrb.sh
```

In Recovery, mount/unlock the installed Data volume as needed, open Terminal, and use its actual volume name and the working Mac's URL:

```sh
sh "/Volumes/<Data-volume-name>/Users/Shared/mcrb.sh" "http://<working-Mac-LAN-IP>:8765"
```

This absolute path is independent of Terminal's starting directory. It requires the Data volume to be accessible and does not select where worker scratch files are written.

If the staged script is unavailable, the server's `/b` endpoint supplies the worker with its URL as the default. After checking that `/tmp` in this Recovery session is suitable scratch storage:

```sh
curl --noproxy '*' -f "http://<working-Mac-LAN-IP>:8765/b" -o /tmp/mcrb.sh && sh /tmp/mcrb.sh
```

The download must succeed before execution. A Recovery reboot requires starting the worker again.

## Inspect storage and verify a round trip

The worker uploads preflight information without creating a spool, then waits. In another Terminal on the working Mac, set `MCRB_REPO` and `MCRB_STATE` to the values above, inspect the mount report, and select verified worker storage:

```sh
python3 "$MCRB_REPO/src/bridge.py" status
cat "$MCRB_STATE/preflight.txt"
python3 "$MCRB_REPO/src/bridge.py" select-temp /private/tmp
printf 'uname -a\n' | python3 "$MCRB_REPO/src/bridge.py" submit-and-wait -
```

`/private/tmp` is an example, not an assertion that it is volatile in every Recovery environment. Check the report's filesystem usage, mounts and disk information. Supported paths are `/tmp`, `/private/tmp`, `/private/var/tmp`, `/System/Volumes/Data/private/tmp` and `/System/Volumes/Data/private/var/tmp`; the worker also checks the resolved physical path. A path's name alone does not identify its backing filesystem. A changed startup report clears the previous selection.

The CLI talks to `http://127.0.0.1:8765` by default. For another port, put `--url http://127.0.0.1:PORT` before the command. `BRIDGE_LOCAL_TEST=1` bypasses preflight for local tests only; do not use it to skip Recovery storage inspection.

The `uname` result should identify the Recovery Mac. Supplied commands execute with the worker's privileges, normally root in Recovery. mcrb transports scripts; it does not choose repairs.

## Submit, inspect and stop

| Command | Purpose |
| --- | --- |
| `status` | Worker heartbeat, state, jobs and results location |
| `select-temp PATH` | Select storage after inspecting preflight |
| `submit FILE --timeout 60` | Queue a shell script and return its job ID |
| `submit-and-wait FILE --timeout 60 --wait-seconds 120` | Queue a script and wait for its result |
| `wait ID --wait-seconds 120` | Continue waiting for an existing job |
| `stop` | Ask the worker to stop, interrupting any active job |

Use `-` instead of a filename to read a script from stdin. An expired wait does not cancel the job. CLI exit codes are 0 for completed, 1 for failed or uncertain execution, and 2 when waiting ends before a result arrives. The remote command's exit code is recorded separately.

Each job directory retains its script, metadata, stdout, stderr and uploaded artifacts. A job can write files into `$BRIDGE_ARTIFACT_DIR` for upload; names may contain letters, digits, underscores, dots and hyphens. An `unknown` result requires inspecting the evidence before issuing another command, not blindly resubmitting the job.

Finish with `python3 "$MCRB_REPO/src/bridge.py" stop`. Verify the worker prints `Worker stopped`, then stop the server with Ctrl-C. Results stay on the working Mac. The worker retains a small temporary spool for inspection; its lifetime depends on Recovery's backing filesystem.

## Validation and provenance

```sh
python3 -m unittest discover -s tests -v
sh -n src/worker.sh
git diff --check
```

Developed during an Apple silicon macOS Recovery investigation of a boot/login hang. The worker and command bridge were used in Recovery; the automated suite runs on normal macOS. Check the mounted Data-volume bootstrap path on each target Mac.

See [AGENTS.md](AGENTS.md) for the repository map and validation commands.

## License

[MIT](LICENSE), copyright 2026 Tom Leonard.
