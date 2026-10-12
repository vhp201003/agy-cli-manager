# Phase 2: CLI Subcommands & Shortcuts

## Scope
- Implement `agy-cli-manager server [start|status|accounts|service]` subcommand group.
- Implement `agy-cli-manager client [connect|status|disconnect|dashboard|test]` subcommand group.
- Register binary aliases in `pyproject.toml`:
  - `agy-server = "agy_cli_manager.cli:server_entrypoint"`
  - `agy-client = "agy_cli_manager.cli:client_entrypoint"`
  - `agy-run = "agy_cli_manager.cli:run_entrypoint"`
- Maintain backward compatibility with legacy `proxy`, `run`, `status`.

## Tasks
- [ ] Add `server` and `client` subparser commands in `cli.py`.
- [ ] Implement `handle_client_connect()`, `handle_client_status()`, `handle_client_disconnect()`.
- [ ] Implement `server_entrypoint()` and `client_entrypoint()`.
- [ ] Update `pyproject.toml` scripts with `agy-server` and `agy-client`.
