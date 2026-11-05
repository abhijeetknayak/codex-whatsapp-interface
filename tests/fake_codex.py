#!/usr/bin/env python3
"""Minimal Codex process simulator for offline smoke tests."""
import json
import sys

def output(obj):
    print(json.dumps(obj), flush=True)

if sys.argv[1:2] == ["app-server"]:
    for line in sys.stdin:
        msg = json.loads(line)
        method = msg.get("method")
        rid = msg.get("id")
        if rid is None:
            continue
        if method == "initialize":
            output({"id": rid, "result": {"userAgent": "fake"}})
        elif method == "model/list":
            output({"id": rid, "result": {"data": [
                {"id": "mock-pro", "model": "mock-pro", "displayName": "Mock Pro",
                 "isDefault": True, "supportedReasoningEfforts": [
                    {"reasoningEffort": "medium"}, {"reasoningEffort": "high"}]},
            ], "nextCursor": None}})
        elif method == "account/rateLimits/read":
            output({"id": rid, "result": {"rateLimitsByLimitId": {"codex": {
                "primary": {"usedPercent": 13, "windowDurationMins": 300, "resetsAt": 1793808000},
                "secondary": {"usedPercent": 42, "windowDurationMins": 10080, "resetsAt": 1793808000}
            }}}})
        elif method == "thread/list":
            output({"id": rid, "result": {"data": [
                {"id": "01a1a111-1111-1111-1111-111111111111",
                 "name": "Genuine archived title", "preview": "old user message"},
            ], "nextCursor": None}})
        elif method == "thread/turns/list":
            output({"id": rid, "result": {"data": [{"items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "What is my project?"}]},
                {"type": "agentMessage", "phase": "final_answer", "text": "It's a mock project."}
            ]}]}})
        else:
            output({"id": rid, "error": {"message": "unsupported"}})
elif sys.argv[1:2] == ["exec"]:
    prompt = sys.stdin.read()
    output({"type": "thread.started", "thread_id": "01a1a111-1111-1111-1111-111111111111"})
    output({"type": "turn.started"})
    if "warning" in prompt.lower():
        output({"type": "item.completed", "item": {"type": "error", "message": "Deprecated feature flag"}})
    output({"type": "item.completed", "item": {"type": "agent_message", "text": "Mock response: " + prompt}})
    output({"type": "turn.completed", "usage": {"input_tokens": 23, "output_tokens": 14}})
else:
    print("Unknown fake command", file=sys.stderr)
    sys.exit(1)
