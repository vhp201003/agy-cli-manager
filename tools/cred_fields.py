import base64
import json
import pathlib

root = pathlib.Path.home() / ".agy-cli-manager/accounts"
for acc in ("acc1", "acc2", "acc3"):
    base = root / acc / ".gemini/antigravity-cli"
    print("==", acc)
    for fn in ("antigravity-oauth-token", "credential.json"):
        f = base / fn
        if not f.exists():
            print("  ", fn, "MISSING")
            continue
        d = json.loads(f.read_text().strip())
        inner = d.get("token") if isinstance(d.get("token"), dict) else d
        idt = d.get("id_token") or inner.get("id_token") or ""
        claim = {}
        if idt:
            part = idt.split(".")[1]
            part += "=" * (-len(part) % 4)
            claim = json.loads(base64.urlsafe_b64decode(part))
        print("  ", fn)
        print("     plain email   :", d.get("email") or inner.get("email"))
        print("     id_token email:", claim.get("email"))
        print("     id_token iss  :", claim.get("iss"))
