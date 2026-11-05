#!/usr/bin/env python3
"""Simulates Codex app-server lifecycle + human approvals, without an account."""
import json
import sys
import time


def send(obj):
    print(json.dumps(obj, separators=(",", ":")), flush=True)


prompt = ""
method = ""
thread_id = "thread-fake-123"
turn_id = "turn-fake-456"
waiting = False
for line in sys.stdin:
    try:
        obj = json.loads(line)
    except ValueError:
        continue
    rid = obj.get("id")
    meth = obj.get("method")
    if meth == "initialize":
        if "requiredHumanApproval" in obj.get("params", {}).get("capabilities", {}):
            send({"id": rid, "error": {"message": "unknown initialization capability"}})
            continue
        send({"id": rid, "result": {"userAgent": "fake-server"}})
    elif meth in ("thread/start", "thread/resume"):
        if "approvalsReviewer" in obj.get("params", {}):
            send({"id": rid, "error": {"message": "unknown thread parameter"}})
            continue
        if meth == "thread/resume":
            thread_id = obj.get("params", {}).get("threadId") or thread_id
        send({"id": rid, "result": {"thread": {"id": thread_id}}})
    elif meth == "turn/start":
        if "approvalsReviewer" in obj.get("params", {}):
            send({"id": rid, "error": {"message": "unknown turn parameter"}})
            continue
        prompt = obj["params"]["input"][0]["text"]
        if prompt.startswith("request_command"):
            method = "item/commandExecution/requestApproval"
            item = {"type": "commandExecution", "id": "exec-fake", "command": "echo hello", "cwd": "/fake", "status": "inProgress"}
        elif prompt.startswith("request_file"):
            method = "item/fileChange/requestApproval"
            item = {"type": "fileChange", "id": "exec-fake", "status": "inProgress",
                    "changes": [{"path": "README.md", "kind": "update", "diff": "+new line"}]}
        elif prompt.startswith("request_permissions"):
            method = "item/permissions/requestApproval"
            item = {"type": "commandExecution", "id": "exec-fake", "status": "inProgress"}
        else:
            method = ""
            item = None
        send({"id": rid, "result": {"turn": {"id": turn_id, "status": "inProgress"}}})
        if item:
            send({"method": "item/started", "params": {"item": item, "threadId": thread_id, "turnId": turn_id}})
            params = {"threadId": thread_id, "turnId": turn_id, "itemId": "exec-fake", "cwd": "/fake", "reason": "Run a test action"}
            if method == "item/commandExecution/requestApproval":
                params["command"] = "echo hello"
                params["kind"] = "command"
            if method == "item/permissions/requestApproval":
                params["permissions"] = {"network": {"enabled": True}, "fileSystem": None}
            send({"id": 997, "method": method, "params": params})
            waiting = True
        elif prompt == "sleep_forever":
            while True:
                time.sleep(0.1)
        else:
            send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
                  "item": {"id": "reply", "type": "agentMessage", "text": "Mock: " + prompt, "phase": "final_answer"}}})
            send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "error": None, "items": []}}})
    elif rid == 997 and "result" in obj and waiting:
        result = obj["result"]
        allowed = (result.get("decision") == "accept") if method != "item/permissions/requestApproval" else bool(result.get("permissions"))
        text = "APPROVED" if allowed else "DECLINED"
        send({"method": "item/completed", "params": {"threadId": thread_id, "turnId": turn_id,
              "item": {"id": "reply", "type": "agentMessage", "text": text, "phase": "final_answer"}}})
        send({"method": "turn/completed", "params": {"threadId": thread_id, "turn": {"id": turn_id, "status": "completed", "error": None, "items": []}}})
        waiting = False
