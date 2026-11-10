import json, pathlib, base64, sys

def email_of(tok):
    p = tok.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p)).get("email")

root = pathlib.Path.home() / ".agy-cli-manager/accounts"
for a in ("acc1", "acc2", "acc3"):
    base = root / a / ".gemini/antigravity-cli"
    print("==", a)
    for fn in ("antigravity-oauth-token", "credential.json"):
        f = base / fn
        if not f.exists():
            print("  ", fn, "MISSING")
            continue
        try:
            d = json.loads(f.read_text().strip())
        except Exception as exc:
            print("  ", fn, "non-json", exc)
            continue
        inner = d.get("token") if isinstance(d.get("token"), dict) else d
        tok = inner.get("access_token") or ""
        rt = inner.get("refresh_token") or ""
        try:
            em = email_of(tok) if tok else None
        except Exception:
            em = "decode-err"
        print("  ", fn, "email=", em, "rt_tail=", rt[-6:], "exp=", inner.get("expiry"))
