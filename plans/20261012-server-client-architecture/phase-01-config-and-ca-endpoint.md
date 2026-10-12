# Phase 1: Client/Server Configuration & CA Distribution Endpoint

## Scope
- Implement client configuration store in `~/.agy-cli-manager/config.json`.
- Support reading `proxy_host`, `proxy_port`, `gateway_url`, and `mode`.
- Add `GET /ca.crt` endpoint in `fastapi_app.py` returning root CA certificate.
- Update `run_agy_wrapper` to resolve proxy endpoint with hierarchy:
  1. `CLI args` / `AGY_PROXY_URL` env var
  2. `config.json` (`gateway` or `proxy_endpoint`)
  3. Default (`127.0.0.1:8899`)

## Tasks
- [ ] Add `load_client_config()`, `save_client_config()`, `reset_client_config()` in `manager.py`.
- [ ] Add `GET /ca.crt` in `fastapi_app.py` serving `ca.crt`.
- [ ] Update `run_agy_wrapper` in `manager.py` to use resolved endpoint and bundle path.
