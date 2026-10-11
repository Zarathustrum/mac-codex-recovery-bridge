# Working in mac-codex-recovery-bridge

Keep this a small, manually started Recovery command bridge. Preserve the Python standard-library server and POSIX shell worker. Scope changes to the requested behavior; avoid adding framework or service infrastructure.

## Layout

- `src/bridge.py`: HTTP server and command interface on the working Mac.
- `src/worker.sh`: self-contained Recovery-side shell worker.
- `tests/test_bridge.py`: integration tests using loopback sockets and harmless local jobs; tests remove their own worker spools.
- `sessions/`: generated investigation state, ignored by Git. Retain results until the operator disposes of them.
- `README.md`: setup, operation, access model and validation.

## Validation

```sh
python3 -m unittest discover -s tests -v
sh -n src/worker.sh
git diff --check
```

Tests run on normal macOS and require loopback listening sockets. Do not test repairs against real installed-system paths. A successful local suite does not verify every Recovery version or a target's mounted Data-volume path.
