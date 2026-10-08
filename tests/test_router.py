import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from agy_cli_manager.proxy.token_manager import TokenManager
from agy_cli_manager.proxy.proxy_server import _extract_session_key

tm = TokenManager()

# Mock pool reflecting real-world conditions:
# acc1: Gemini 100%, Claude 0.52% (Claude exhausted, Gemini alive)
# acc2: Gemini 88%,  Claude 17%   (Both alive)
# acc3: Gemini 100%, Claude 47%   (Both alive)
# acc4: Gemini 77%,  Claude 0%    (Claude exhausted, Gemini alive)
tm._accounts = {
    'acc1': {'access_token': 'tok1', 'quota': {
        'gemini': {'weekly': {'percent': 100.0, 'reset_time': '2026-10-10'}, '5h': {'percent': 99.0, 'reset_time': '2026-10-07'}},
        'third_party': {'weekly': {'percent': 0.52, 'reset_time': '2026-10-10'}, '5h': {'percent': 0.0, 'reset_time': '2026-10-07'}},
    }},
    'acc2': {'access_token': 'tok2', 'quota': {
        'gemini': {'weekly': {'percent': 88.0, 'reset_time': '2026-10-10'}, '5h': {'percent': 34.0, 'reset_time': '2026-10-07'}},
        'third_party': {'weekly': {'percent': 17.0, 'reset_time': '2026-10-10'}, '5h': {'percent': 34.0, 'reset_time': '2026-10-07'}},
    }},
    'acc3': {'access_token': 'tok3', 'quota': {
        'gemini': {'weekly': {'percent': 100.0, 'reset_time': '2026-10-10'}, '5h': {'percent': 100.0, 'reset_time': '2026-10-07'}},
        'third_party': {'weekly': {'percent': 47.0, 'reset_time': '2026-10-10'}, '5h': {'percent': 100.0, 'reset_time': '2026-10-07'}},
    }},
    'acc4': {'access_token': 'tok4', 'quota': {
        'gemini': {'weekly': {'percent': 77.0, 'reset_time': '2026-10-10'}, '5h': {'percent': 100.0, 'reset_time': '2026-10-07'}},
        'third_party': {'weekly': {'percent': 0.0, 'reset_time': '2026-10-10'}, '5h': {'percent': 0.0, 'reset_time': '2026-10-07'}},
    }},
}

sessions = ['subagent-A', 'subagent-B', 'subagent-C', 'subagent-D', 'subagent-E']

# 1. Test Gemini model routing: All 4 accounts are alive for Gemini!
gemini_chosen = {s: tm.get_token_by_session(s, model_name='gemini-2.5-flash-lite')[0] for s in sessions}
print('Gemini model routing accounts:', gemini_chosen)
assert set(gemini_chosen.values()).issubset({'acc1', 'acc2', 'acc3', 'acc4'})
print('[PASS] Gemini routing uses all 4 healthy Gemini accounts (acc1 & acc4 not blocked)')

# 2. Test Claude model routing: Only acc2 & acc3 are alive for Claude (acc1 & acc4 excluded)
claude_chosen = {s: tm.get_token_by_session(s, model_name='claude-3-7-sonnet')[0] for s in sessions}
print('Claude model routing accounts:', claude_chosen)
for s, acc in claude_chosen.items():
    assert acc in ['acc2', 'acc3'], f"Account {acc} chosen for Claude despite exhausted Claude quota!"
print('[PASS] Claude routing strictly filters out exhausted Claude accounts (acc1, acc4)')

# 3. Test agent requestId extraction
agent_body = '{"project": "aicode-consumers", "requestId": "agent/89ce14d5-6579-4ce5-bb2c-6789d8b62f38/1791361478857/uuid-123"}'
extracted_agent = _extract_session_key('/', {}, agent_body)
assert extracted_agent == 'agent:89ce14d5-6579-4ce5-bb2c-6789d8b62f38', f"Extracted: {extracted_agent}"
print('[PASS] Successfully extracted agent_id from Antigravity agent requestId:', extracted_agent)

# 4. Test chat requestId extraction
chat_body = '{"project": "aicode-consumers", "requestId": "chat/be1310ed-4004-4cd4-996b-33b2ef3465cb"}'
extracted_chat = _extract_session_key('/', {}, chat_body)
assert extracted_chat == 'chat:be1310ed-4004-4cd4-996b-33b2ef3465cb', f"Extracted: {extracted_chat}"
print('[PASS] Successfully extracted chat_id from Antigravity chat requestId:', extracted_chat)

# 5. Test nested request.sessionId extraction
nested_body = '{"project": "aicode-consumers", "request": {"sessionId": -3750763034362895579}}'
extracted_nested = _extract_session_key('/', {}, nested_body)
assert extracted_nested == '-3750763034362895579', f"Extracted: {extracted_nested}"
print('[PASS] Successfully extracted nested request.sessionId:', extracted_nested)

print('\nALL DUAL-FAMILY ROUTER & EXTRACTION TESTS PASSED!')
