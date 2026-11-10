import json, pathlib, urllib.error, urllib.parse, urllib.request

ROOT = pathlib.Path.home() / ".agy-cli-manager"


def env(key: str) -> str:
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise KeyError(key)


def refresh(refresh_token: str) -> tuple[str | None, str]:
    data = urllib.parse.urlencode({
        "client_id": env("GOOGLE_CLIENT_ID"),
        "client_secret": env("GOOGLE_CLIENT_SECRET"),
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    try:
        body = urllib.request.urlopen(
            urllib.request.Request("https://oauth2.googleapis.com/token", data=data), timeout=30
        ).read()
        return json.loads(body)["access_token"], ""
    except urllib.error.HTTPError as exc:
        return None, f"HTTP {exc.code}: {exc.read().decode()[:120]}"


def userinfo(access_token: str) -> str:
    req = urllib.request.Request(
        "https://www.googleapis.com/oauth2/v3/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read()).get("email", "?")
    except urllib.error.HTTPError as exc:
        return f"userinfo HTTP {exc.code}"


for acc in ("acc1", "acc2", "acc3"):
    store = ROOT / "accounts" / acc / ".gemini/antigravity-cli/antigravity-oauth-token"
    payload = json.loads(store.read_text().strip())
    inner = payload.get("token") if isinstance(payload.get("token"), dict) else payload
    access_token, err = refresh(inner.get("refresh_token", ""))
    if not access_token:
        print(acc, "refresh failed:", err)
        continue
    print(acc, "->", userinfo(access_token))
