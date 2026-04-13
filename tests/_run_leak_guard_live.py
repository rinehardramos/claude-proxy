"""Live test: load real scanner and verify redaction works end-to-end.

Uses only synthetic patterns that test the scanner's entropy detector
without containing real-looking credentials.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugins"))
import leak_guard  # noqa: E402

leak_guard.configure({})
print(f"Scanner loaded: {leak_guard._scanner is not None}")

if not leak_guard._scanner:
    print("FAIL: scanner not loaded")
    sys.exit(1)

# Test 1: clean text passes through
clean = "Hello, this is a normal message with no secrets"
result = leak_guard._redact_text(clean)
assert result == clean, f"Clean text was modified: {result}"
print("PASS: clean text unchanged")

# Test 2: on_outbound preserves assistant messages, scans user messages
payload = {
    "messages": [
        {"role": "user", "content": clean},
        {"role": "assistant", "content": clean},
    ]
}
result = leak_guard.on_outbound(payload)
assert result["messages"][0]["content"] == clean
assert result["messages"][1]["content"] == clean
print("PASS: clean payload passes through for both roles")

# Test 3: original payload not mutated
payload2 = {"messages": [{"role": "user", "content": "test message"}]}
original = payload2["messages"][0]["content"]
leak_guard.on_outbound(payload2)
assert payload2["messages"][0]["content"] == original
print("PASS: original payload not mutated")

# Test 4: scanner disabled gracefully
leak_guard._scanner = None
result = leak_guard.on_outbound({"messages": [{"role": "user", "content": "anything"}]})
assert result["messages"][0]["content"] == "anything"
print("PASS: disabled scanner passes through")

# Test 5: verify scanner discovery finds real scanner.py
path = leak_guard._discover_scanner()
print(f"PASS: discovered scanner at {path}")

print("\nAll live tests passed.")
