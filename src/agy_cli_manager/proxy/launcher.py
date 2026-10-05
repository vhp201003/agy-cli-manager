from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path
import uvicorn

from agy_cli_manager.proxy.cert_manager import CertManager
from agy_cli_manager.proxy.token_manager import TokenManager
from agy_cli_manager.proxy.proxy_server import start_proxy_server
from agy_cli_manager.proxy.fastapi_app import app as fastapi_app

logger = logging.getLogger("AgyProxy.Launcher")


def run_proxy_service(
    proxy_host: str = "127.0.0.1",
    proxy_port: int = 8899,
    dashboard_host: str = "127.0.0.1",
    dashboard_port: int = 8800,
    open_browser: bool = True,
) -> None:
    cm = CertManager()
    bundle_path = cm.get_ca_bundle_path()
    cm.install_ca_windows()

    tm = TokenManager()
    tm.reload_accounts()
    accounts = list(tm._accounts.keys())

    print("=" * 68)
    print("      [AGY-CLI-MANAGER] MULTI-ACCOUNT ROUTER & FASTAPI TELEMETRY")
    print("=" * 68)
    print(f"[*] Intercepting Proxy Port : http://{proxy_host}:{proxy_port}")
    print(f"[*] Live Web Dashboard     : http://{dashboard_host}:{dashboard_port}")
    print(f"[*] Swagger API Docs       : http://{dashboard_host}:{dashboard_port}/docs")
    print(f"[*] Trusted CA Bundle      : {bundle_path}")
    print(f"[*] Discovered Accounts    : {accounts}")
    print(f"[*] Auto-Refresh Cron      : Active (proactive check every 15 mins)")
    print("-" * 68)
    print("To hook AGY CLI in another terminal, run:")
    print(f'  $env:HTTPS_PROXY   = "http://{proxy_host}:{proxy_port}"')
    print(f'  $env:HTTP_PROXY    = "http://{proxy_host}:{proxy_port}"')
    print(f'  $env:SSL_CERT_FILE = "{bundle_path}"')
    print("  agy")
    print("=" * 68)

    # Start Proxy Server in background thread
    server, proxy_thr = start_proxy_server(port=proxy_port)

    if open_browser:
        import webbrowser
        def _open():
            time.sleep(1.2)
            webbrowser.open(f"http://{dashboard_host}:{dashboard_port}")
        threading.Thread(target=_open, daemon=True).start()

    try:
        uvicorn.run(
            fastapi_app,
            host=dashboard_host,
            port=dashboard_port,
            log_level="warning",
        )
    except KeyboardInterrupt:
        pass
    finally:
        print("\nShutting down Proxy server...")
        server.shutdown()
        server.server_close()
        print("Done.")
