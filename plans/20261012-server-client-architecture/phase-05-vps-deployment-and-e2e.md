# Phase 5: VPS Deployment, Service Rebuild & Local E2E Verification

## Scope
- Commit and push changes to GitHub (`origin main`).
- SSH into VPS, pull latest code, reinstall editable package with new entrypoints (`agy-server`, `agy-client`).
- Restart systemd service (`agy-proxy.service`).
- Verify from local Windows machine:
  - Run `agy-client connect http://192.168.40.24:8800`.
  - Check `agy-client status`.
  - Execute `agy-run` test.

## Tasks
- [ ] Push to GitHub.
- [ ] SSH to VPS: `git pull`, `pip install -e ".[proxy]"`.
- [ ] Rebuild/restart systemd service on VPS.
- [ ] Run `agy-client connect` from local and verify live connection.
