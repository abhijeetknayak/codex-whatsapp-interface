"""Interactive Codex app-server runner with *human* WhatsApp approvals.

Unlike `codex exec`, app-server supports server-initiated approval requests.
Each job starts its own app-server subprocess, persists pending approvals to
SQLite, and waits for a decision from the verified WhatsApp sender.  No action
is approved automatically, and all unhandled requests fail closed.
"""
from __future__ import annotations

import json
import logging
import os
import select
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from codex_api import CodexCancelled, CodexError, EFFORTS, MODEL_ID

LOG = logging.getLogger(__name__)
APPROVAL_METHODS = {
    "item/commandExecution/requestApproval": "command",
    "item/fileChange/requestApproval": "file",
    "item/permissions/requestApproval": "permissions",
}


def approval_result(method: str, approved: bool, params: dict) -> dict:
    """Grant one action, never a reusable/always-allow rule."""
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        return {"decision": "accept" if approved else "decline"}
    if method == "item/permissions/requestApproval":
        requested = params.get("permissions") or {}
        # Only grant the exact requested permissions, for THIS turn, on consent.
        result = {key: requested[key] for key in ("network", "fileSystem")
                  if approved and isinstance(requested.get(key), dict)}
        return {"permissions": result, "scope": "turn"}
    raise CodexError(f"Unsupported approval method: {method}")


def describe_approval(method: str, params: dict, item: dict | None = None) -> str:
    """Make a reviewable description; don't obscure a dangerous long command."""
    kind = APPROVAL_METHODS.get(method, "unknown")
    cwd = params.get("cwd") or "(not supplied)"
    reason = params.get("reason") or "No explanation supplied by Codex"
    if kind == "command":
        network = params.get("networkApprovalContext") or {}
        if network:
            host = network.get("host") or "unknown"
            protocol = network.get("protocol") or "unknown"
            port = network.get("port")
            action = f"Network access to {host} via {protocol}" + (f" port {port}" if port is not None else "")
        else:
            action = params.get("command") or (item or {}).get("command")
            if not isinstance(action, str) or not action.strip():
                raise CodexError("Command text unavailable; refusing blind approval")
        if params.get("additionalPermissions"):
            action += "\nRequested elevated permissions: " + json.dumps(params["additionalPermissions"], ensure_ascii=True)
    elif kind == "file":
        changes = (item or {}).get("changes", [])
        if not changes:
            raise CodexError("File change details missing; refusing blind approval")
        parts = []
        for change in changes:
            if not isinstance(change, dict):
                raise CodexError("Invalid file change details")
            path = str(change.get("path") or "(unknown)")
            diff = change.get("diff")
            if not isinstance(diff, str) or not diff:
                raise CodexError("File diff unavailable; refusing blind approval")
            parts.append(f"{path}:\n{diff}")
        root = params.get("grantRoot")
        action = "File edits:\n" + "\n".join(parts)
        if root:
            action += f"\nRequested root: {root}"
    else:
        action = "Additional permissions: " + json.dumps(params.get("permissions") or {}, ensure_ascii=True)
    # Don't allow the user to approve commands that were truncated in the UI.
    if len(action) > 1400 or len(str(reason)) > 400:
        raise CodexError("Approval details are too long to display safely on WhatsApp")
    return f"Type: {kind}\nWorking directory: {cwd}\nAction: {action}\nReason: {reason}"


class AppServerRunner:
    """One Codex app-server process per job.  Do not reuse across senders."""

    def __init__(self, codex_bin: str, cwd: Path, timeout: int, cancel: Callable[[], bool],
                 store, turn: dict, sandbox: str, approval_timeout: int):
        self.bin = codex_bin
        self.cwd = Path(cwd)
        self.timeout = timeout
        self.cancel = cancel
        self.store = store
        self.turn = turn
        self.sandbox = sandbox
        self.approval_timeout = approval_timeout
        self.process: subprocess.Popen | None = None
        self.stderr = None
        self.buf = b""
        self.next_id = 10
        self.started_at = time.monotonic()
        self.thread_id = None
        self.turn_id = None
        self.final = []
        self.nonfinal = []
        self.items = {}
        self.usage = {"input_tokens": 0, "output_tokens": 0}
        self.completed_turn = None

    def _write(self, data: dict):
        assert self.process is not None and self.process.stdin is not None
        self.process.stdin.write((json.dumps(data, separators=(",", ":")) + "\n").encode("utf-8"))
        self.process.stdin.flush()

    def _check(self):
        if self.cancel():
            raise CodexCancelled("Cancelled by WhatsApp user")
        if time.monotonic() - self.started_at > self.timeout:
            raise CodexError("Codex task timeout; changes already made may remain")
        if self.process is not None and self.process.poll() is not None:
            raise CodexError(f"Codex app-server exited unexpectedly: {self.process.returncode}")

    def _read(self, max_wait: float = 0.4) -> dict | None:
        assert self.process is not None and self.process.stdout is not None
        self._check()
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if line.strip():
                try:
                    obj = json.loads(line)
                except ValueError:
                    LOG.warning("Ignoring non-JSON app-server output")
                    continue
                if isinstance(obj, dict):
                    return obj
        ready, _, _ = select.select([self.process.stdout], [], [], max_wait)
        if ready:
            data = os.read(self.process.stdout.fileno(), 65536)
            if not data:
                raise CodexError("Codex app-server closed its output")
            self.buf += data
            if len(self.buf) > 8_000_000:
                raise CodexError("Codex app-server sent an oversized response")
            return self._read(0)
        return None

    def _request(self, method: str, params: dict, deadline: float = 25) -> dict:
        req_id = self.next_id
        self.next_id += 1
        self._write({"id": req_id, "method": method, "params": params})
        expires = time.monotonic() + deadline
        while True:
            if time.monotonic() > expires:
                raise CodexError(f"Codex app-server {method} didn't answer")
            obj = self._read()
            if not obj:
                continue
            if obj.get("id") == req_id and "method" not in obj:
                if obj.get("error"):
                    raise CodexError(f"{method} failed: {str(obj['error'])[:700]}")
                return obj.get("result") or {}
            self._dispatch(obj)

    def _dispatch(self, obj: dict):
        method = obj.get("method")
        if not isinstance(method, str):
            return
        params = obj.get("params") or {}
        if "id" in obj:   # Codex -> client request (approval or unsupported feature)
            if method not in APPROVAL_METHODS:
                LOG.warning("Declining unsupported Codex request: %s", method)
                self._write({"id": obj["id"], "error": {
                    "code": -32601, "message": "This WhatsApp client cannot handle that request"}})
                return
            self._approval(obj["id"], method, params)
            return
        if method in ("item/started", "item/completed"):
            item = params.get("item") or {}
            if not isinstance(item, dict):
                return
            iid = item.get("id")
            if iid:
                self.items[iid] = item
            if method == "item/completed" and item.get("type") == "agentMessage":
                body = item.get("text")
                if isinstance(body, str) and body:
                    if item.get("phase") == "final_answer":
                        self.final.append(body)
                    elif item.get("phase") != "commentary":
                        self.nonfinal.append(body)
        elif method == "thread/tokenUsage/updated":
            stats = (params.get("tokenUsage") or {}).get("last") or {}
            self.usage = {
                "input_tokens": int(stats.get("inputTokens") or 0),
                "output_tokens": int(stats.get("outputTokens") or 0),
            }
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            if not self.turn_id or turn.get("id") == self.turn_id:
                self.completed_turn = turn
                for item in turn.get("items", []):
                    if isinstance(item, dict) and item.get("type") == "agentMessage" and item.get("text"):
                        if item.get("phase") == "final_answer" and item["text"] not in self.final:
                            self.final.append(item["text"])

    def _approval(self, request_id, method: str, params: dict):
        if params.get("threadId") and self.thread_id and params["threadId"] != self.thread_id:
            LOG.warning("Mismatched thread in approval request; declining")
            self._write({"id": request_id, "result": approval_result(method, False, params)})
            return
        if params.get("turnId") and self.turn_id and params["turnId"] != self.turn_id:
            LOG.warning("Mismatched turn in approval request; declining")
            self._write({"id": request_id, "result": approval_result(method, False, params)})
            return
        if method == "item/commandExecution/requestApproval" and params.get("kind") == "humanCommand":
            LOG.info("Managed human command approval received")
        try:
            details = describe_approval(method, params, self.items.get(params.get("itemId")))
        except CodexError:
            LOG.warning("Approval details too large: denying without forwarding")
            self._write({"id": request_id, "result": approval_result(method, False, params)})
            return
        approval_id = self.store.create_approval(
            self.turn, method, str(request_id), details, self.approval_timeout)
        LOG.info("Waiting for approval #%s, task #%s", approval_id, self.turn["id"])
        decision = "pending"
        expires = time.monotonic() + self.approval_timeout
        while time.monotonic() < expires:
            self._check()
            decision = self.store.approval_decision(approval_id)
            if decision in ("approved", "denied", "expired", "cancelled", "stale"):
                break
            time.sleep(0.25)
        if decision == "pending":
            self.store.expire_approval(approval_id)
            decision = "expired"
        allow = decision == "approved"
        self._write({"id": request_id, "result": approval_result(method, allow, params)})

    def run(self, prompt: str, prior_thread: str | None, model: str | None, effort: str | None) -> dict:
        if self.sandbox not in ("read-only", "workspace-write", "inherit"):
            raise CodexError("Sandbox must be workspace-write, read-only or inherit")
        if model and not MODEL_ID.fullmatch(model):
            raise CodexError("Invalid model ID")
        if effort and effort not in EFFORTS:
            raise CodexError("Unsupported reasoning effort")
        self._check()
        self.stderr = tempfile.TemporaryFile()
        self.process = subprocess.Popen([self.bin, "app-server"], cwd=self.cwd,
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.stderr, bufsize=0,
                                        start_new_session=(os.name == "posix"))
        try:
            self._request("initialize", {
                "clientInfo": {"name": "whatsapp_codex", "title": "WhatsApp Codex Bot", "version": "2.3.0"},
                "capabilities": {"experimentalApi": True},
            })
            self._write({"method": "initialized", "params": {}})
            if prior_thread:
                # On resume, only pass stable schema fields. Turn-specific policy
                # is supplied on turn/start, avoiding stored legacy settings.
                result = self._request("thread/resume", {
                    "threadId": prior_thread, "excludeTurns": True,
                }, deadline=45)
            else:
                settings = {"cwd": str(self.cwd), "approvalPolicy": "on-request"}
                if self.sandbox != "inherit":
                    settings["sandbox"] = self.sandbox
                if model:
                    settings["model"] = model
                result = self._request("thread/start", settings, deadline=45)
            self.thread_id = (result.get("thread") or {}).get("id") or prior_thread
            if not self.thread_id:
                raise CodexError("Codex app-server didn't return thread ID")
            turn_params = {"threadId": self.thread_id, "input": [{"type": "text", "text": prompt}],
                           "cwd": str(self.cwd), "approvalPolicy": "on-request"}
            if model:
                turn_params["model"] = model
            if effort:
                turn_params["effort"] = effort
            if self.sandbox != "inherit":
                # Restrict writes to the selected project and its contents.
                if self.sandbox == "workspace-write":
                    turn_params["sandboxPolicy"] = {
                        "type": "workspaceWrite", "writableRoots": [str(self.cwd)],
                        "networkAccess": False, "excludeTmpdirEnvVar": False, "excludeSlashTmp": False,
                    }
                else:
                    turn_params["sandboxPolicy"] = {"type": "readOnly", "networkAccess": False}
            started = self._request("turn/start", turn_params, deadline=45)
            self.turn_id = (started.get("turn") or {}).get("id")
            if not self.turn_id:
                raise CodexError("Codex app-server didn't return a turn ID")
            while self.completed_turn is None:
                msg = self._read()
                if msg:
                    self._dispatch(msg)
            status = self.completed_turn.get("status")
            if status != "completed":
                err = self.completed_turn.get("error") or "no details"
                raise CodexError(f"Codex turn {status}: {str(err)[:500]}")
            return {"thread_id": self.thread_id,
                    "response": (self.final or self.nonfinal or ["Codex completed without a text response."])[-1],
                    "input_tokens": self.usage["input_tokens"],
                    "output_tokens": self.usage["output_tokens"]}
        except BaseException:
            raise
        finally:
            self.store.close_turn_approvals(self.turn["id"])
            self._stop_process()

    def _stop_process(self):
        proc = self.process
        if not proc:
            return
        if proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "posix":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)
        for handle in (proc.stdin, proc.stdout, self.stderr):
            try:
                if handle:
                    handle.close()
            except OSError:
                pass


def run_codex_interactive(codex_bin: str, cwd: Path, prompt: str, thread_id: str | None,
                          model: str | None, effort: str | None, timeout: int,
                          should_cancel: Callable[[], bool], store, turn: dict,
                          sandbox: str = "workspace-write", approval_timeout: int = 600) -> dict:
    return AppServerRunner(codex_bin, cwd, timeout, should_cancel, store, turn,
                           sandbox, approval_timeout).run(prompt, thread_id, model, effort)
