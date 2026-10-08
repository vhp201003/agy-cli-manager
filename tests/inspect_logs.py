import urllib.request
import json
import re

try:
    with urllib.request.urlopen('http://127.0.0.1:8800/api/logs', timeout=5) as resp:
        data = json.loads(resp.read().decode('utf-8'))
        reqs = [d for d in data if 'streamGenerateContent' in d.get('path', '')]
        print(f"Total intercepted requests: {len(data)}, streamGenerateContent: {len(reqs)}")
        for d in reqs[-3:]:
            b = d.get('body', '')
            print("---")
            print("Time:", d.get('time'))
            print("Account:", d.get('account'))
            print("Model:", d.get('model'))
            print("Path:", d.get('path'))
            # Print sample body structure
            keys = re.findall(r'"([a-zA-Z0-9_-]+)"\s*:', b[:1000])
            print("Found keys in first 1000 chars:", keys)
            req_id_m = re.search(r'"requestId"\s*:\s*"([^"]+)"', b)
            if req_id_m:
                print("requestId:", req_id_m.group(1))
            session_m = re.search(r'"([a-zA-Z0-9_-]*[sS]ession[a-zA-Z0-9_-]*)"\s*:\s*"([^"]+)"', b)
            if session_m:
                print("Session match:", session_m.group(1), "=", session_m.group(2))
            conv_m = re.search(r'"([a-zA-Z0-9_-]*[cC]onversation[a-zA-Z0-9_-]*)"\s*:\s*"([^"]+)"', b)
            if conv_m:
                print("Conv match:", conv_m.group(1), "=", conv_m.group(2))
except Exception as e:
    print("Error querying proxy:", e)
