"""Codex CLI execution and short-lived app-server JSON-RPC helpers.

Uses the installed Codex binary and its existing login/configuration. Never
adds an approval or sandbox bypass flag. Only supports local execution.
"""
from __future__ import annotations

import json
import logging
import os
import re
import select
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

LOG = logging.getLogger(__name__)
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


class CodexError(RuntimeError):
    pass


class CodexCancelled(CodexError):
    """An explicit user cancellation, not a Codex error."""


def rpc_call(codex_bin: str, method: str, params: dict | None = None, timeout: int = 15) -> dict:
    """Call one documented app-server method via JSON-RPC over stdio.

    A separate short-lived process is used so this bot does not control (or
    share the state of) an interactive Codex process. stdout notifications are
    skipped and the temporary process is always closed.
    """
    proc = subprocess.Popen(
        [codex_bin, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, bufsize=0,
    )
    assert proc.stdin is not None and proc.stdout is not None
    buffer = b""
    deadline = time.monotonic() + timeout

    def send(value: dict):
        proc.stdin.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
        proc.stdin.flush()

    def wait_for_id(req_id: int) -> dict:
        nonlocal buffer
        while True:
            while b"\n" in buffer:
                data, buffer = buffer.split(b"\n", 1)
                if not data.strip():
                    continue
                try:
                    response = json.loads(data)
                except ValueError:
                    continue
                if response.get("id") == req_id:
                    if "error" in response:
                        raise CodexError(f"Codex app-server {method}: {response['error']}")
                    return response.get("result") or {}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexError(f"Codex app-server {method} timed out")
            if proc.poll() is not None:
                raise CodexError(f"Codex app-server exited ({proc.returncode})")
            ready, _, _ = select.select([proc.stdout], [], [], min(remaining, 0.5))
            if ready:
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    raise CodexError("Codex app-server closed stdout")
                buffer += chunk
                if len(buffer) > 8_000_000:
                    raise CodexError("Codex app-server response too large")

    try:
        send({"method": "initialize", "id": 1,
              "params": {"clientInfo": {"name": "whatsapp_codex_bot",
                                        "title": "Personal WhatsApp Codex Bridge",
                                        "version": "2.0.0"}}})
        wait_for_id(1)
        send({"method": "initialized", "params": {}})
        request = {"method": method, "id": 2}
        if params is not None:
            request["params"] = params
        send(request)
        return wait_for_id(2)
    except (OSError, BrokenPipeError) as exc:
        raise CodexError(f"Could not communicate with Codex app-server: {exc}") from exc
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        for handle in (proc.stdin, proc.stdout):
            if handle:
                handle.close()


def _normalize_model_list(raw: list[dict]) -> list[dict]:
    result, seen = [], set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        if entry.get("hidden") is True:
            continue
        visibility = entry.get("visibility", "list")
        if visibility not in (None, "list", "visible", "public"):
            continue
        slug = entry.get("model") or entry.get("slug") or entry.get("id")
        if not isinstance(slug, str) or not MODEL_ID.fullmatch(slug) or slug in seen:
            continue
        seen.add(slug)
        efforts = []
        for item in entry.get("supportedReasoningEfforts", entry.get("supported_reasoning_levels", [])) or []:
            effort = (item.get("reasoningEffort") or item.get("effort")) if isinstance(item, dict) else item
            if effort in EFFORTS and effort not in efforts:
                efforts.append(effort)
        result.append({
            "id": slug,
            "name": str(entry.get("displayName") or entry.get("display_name") or slug)[:90],
            "efforts": efforts,
            "default_effort": entry.get("defaultReasoningEffort") or entry.get("default_reasoning_level"),
            "default": entry.get("isDefault") is True,
            "priority": entry.get("priority", 1000),
        })
    result.sort(key=lambda x: (not x["default"], x["priority"] if isinstance(x["priority"], int) else 1000, x["name"].lower()))
    return result


def load_models(codex_bin: str, codex_home: Path, explicit_models: str = "") -> list[dict]:
    """Live app-server picker first, local cache second, optional .env fallback."""
    try:
        data = rpc_call(codex_bin, "model/list", {"limit": 100, "includeHidden": False})
        models = _normalize_model_list(data.get("data", []))
        if models:
            return models
    except (OSError, CodexError, ValueError) as exc:
        LOG.warning("Live model/list unavailable: %s", exc)

    try:
        cache = json.loads((codex_home / "models_cache.json").read_text(encoding="utf-8"))
        models = _normalize_model_list(cache.get("models", []))
        if models:
            return models
    except (OSError, ValueError, TypeError):
        pass
    return _normalize_model_list([{"slug": slug.strip(), "display_name": slug.strip()}
                                  for slug in explicit_models.split(",") if slug.strip()])


def format_rate_limits(report: dict) -> str:
    """Report only limits returned by Codex; never invent usage percentages."""
    buckets = report.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        one = report.get("rateLimits")
        buckets = {"codex": one} if isinstance(one, dict) else {}

    windows = []
    seen = set()
    for key, info in buckets.items():
        if not isinstance(info, dict):
            continue
        for label in ("primary", "secondary"):
            window = info.get(label)
            if not isinstance(window, dict):
                continue
            minutes = window.get("windowDurationMins")
            try:
                minutes = int(minutes)
            except (ValueError, TypeError):
                continue
            if minutes <= 0:
                continue
            duplicate = (key, minutes)
            if duplicate in seen:
                continue
            seen.add(duplicate)
            windows.append((str(key), minutes, window))

    def line(key: str, minutes: int, data: dict) -> str:
        used = data.get("usedPercent")
        used_text = f"{float(used):.0f}% used / {100-float(used):.0f}% left" if isinstance(used, (int, float)) else "usage not reported"
        stamp = data.get("resetsAt")
        try:
            reset = datetime.fromtimestamp(int(stamp)).astimezone().strftime("%d %b %H:%M %Z") if stamp is not None else "not reported"
        except (ValueError, TypeError, OverflowError, OSError):
            reset = "not reported"
        bucket = f" ({key})" if key not in ("codex", "") else ""
        return f"{used_text}{bucket}; reset: {reset}"

    five = [x for x in windows if x[1] == 300]
    week = [x for x in windows if x[1] == 10080]
    lines = ["Codex account rate limits:",
             "5-hour: " + (" | ".join(line(k, m, w) for k, m, w in five) if five else "Not reported by this account"),
             "Weekly: " + (" | ".join(line(k, m, w) for k, m, w in week) if week else "Not reported by this account")]
    others = [f"{m} min: {line(k,m,w)}" for k, m, w in windows if m not in (300, 10080)]
    if others:
        lines.append("Other reported limits:\n" + "\n".join(others[:8]))
    if not windows:
        lines.append("No quota windows supplied; this account may not be signed in through ChatGPT.")
    return "\n".join(lines)


def account_usage(codex_bin: str) -> str:
    try:
        report = rpc_call(codex_bin, "account/rateLimits/read", timeout=18)
        return format_rate_limits(report)
    except (OSError, CodexError, ValueError) as exc:
        return f"Codex account quota unavailable: {exc}. Try /status in interactive Codex."


def _legacy_server_threads(codex_home: Path, limit: int) -> list[dict]:
    """Fallback for older Codex CLIs: title index + persisted session rollouts."""
    names: dict[str, str] = {}
    index = codex_home / "session_index.jsonl"
    try:
        with index.open(encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    if entry.get("id") and entry.get("thread_name"):
                        names[str(entry["id"])] = " ".join(str(entry["thread_name"]).split())
                except ValueError:
                    continue
    except OSError:
        pass
    # Exact named titles may also be stored in the local state DB; read-only.
    db_file = codex_home / "state_5.sqlite"
    if db_file.exists():
        import sqlite3
        try:
            with sqlite3.connect(db_file.as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
                for sid, title in conn.execute("SELECT id,title FROM threads"):
                    if sid and title and str(title).strip() and str(sid) not in names:
                        names[str(sid)] = str(title).strip()
        except sqlite3.Error:
            pass
    rows = []
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return []
    for path in sessions.rglob("rollout-*.jsonl"):
        try:
            with path.open(encoding="utf-8") as f:
                first = json.loads(f.readline())
            if first.get("type") != "session_meta":
                continue
            meta = first.get("payload") or {}
            sid, cwd = meta.get("id"), meta.get("cwd")
            if not sid:
                continue
            origin = meta.get("source") or meta.get("originator") or ""
            origin_str = json.dumps(origin).lower()
            if any(s in origin_str for s in ("subagent", "guardian", "review", "compact")):
                continue
            title = names.get(str(sid)) or "Unnamed - " + Path(str(cwd or "unknown")).name
            if title.lower() == "guardian review":
                continue
            rows.append({"id": str(sid), "title": title[:120], "cwd": cwd,
                         "updated_at": path.stat().st_mtime})
        except (OSError, ValueError, TypeError):
            continue
    rows.sort(key=lambda x: x["updated_at"], reverse=True)
    return rows[:limit]


def list_server_threads(codex_bin: str, limit: int = 120,
                        codex_home: Path | None = None) -> list[dict]:
    """User-facing sessions from the Codex app-server; fallback to disk metadata."""
    cursor = None
    threads = []
    try:
        for _ in range(5):
            params: dict[str, Any] = {
                "limit": min(40, limit), "sortKey": "updated_at", "sortDirection": "desc",
                "sourceKinds": ["cli", "vscode", "exec", "appServer"],
            }
            if cursor:
                params["cursor"] = cursor
            raw = rpc_call(codex_bin, "thread/list", params, timeout=20)
            for s in raw.get("data", []):
                if not isinstance(s, dict) or not s.get("id") or s.get("ephemeral"):
                    continue
                title = s.get("name") or s.get("preview") or "Untitled chat"
                threads.append({"id": str(s["id"]),
                                "title": " ".join(str(title).split())[:120] or "Untitled chat",
                                "cwd": s.get("cwd") or None,
                                "updated_at": s.get("updatedAt") or s.get("createdAt") or 0})
                if len(threads) >= limit:
                    return threads
            cursor = raw.get("nextCursor")
            if not cursor:
                break
        return threads
    except (OSError, CodexError, ValueError) as exc:
        LOG.warning("Codex thread/list unavailable: %s", exc)
        if codex_home:
            return _legacy_server_threads(codex_home, limit)
        raise


def lookup_session_cwd(codex_home: Path, session_id: str) -> str | None:
    """Fallback to Codex rollout metadata, only for exact known local session id."""
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.is_dir() or not re.fullmatch(r"[a-fA-F0-9-]{32,40}", session_id):
        return None
    for path in sessions_dir.rglob(f"rollout-*{session_id}.jsonl"):
        try:
            with path.open(encoding="utf-8") as fh:
                event = json.loads(fh.readline())
            meta = event.get("payload") if event.get("type") == "session_meta" else None
            if isinstance(meta, dict) and meta.get("id") == session_id:
                return meta.get("cwd")
        except (OSError, ValueError):
            continue
    return None


def run_codex(codex_bin: str, cwd: Path, prompt: str, thread_id: str | None,
              model: str | None, effort: str | None, timeout: int,
              should_cancel: Callable[[], bool] | None = None,
              sandbox: str = "workspace-write") -> dict:
    """Run a Codex turn inside its configured/explicit workspace sandbox.

    CLI sandbox mode is limited to workspace-write/read-only/inherit for a
    WhatsApp-exposed agent. Never disable approvals or use full host access.
    """
    if sandbox not in ("workspace-write", "read-only", "inherit"):
        raise CodexError("Invalid CODEX_SANDBOX_MODE: choose workspace-write, read-only, or inherit")
    cmd = [codex_bin, "exec", "--json"]
    if sandbox != "inherit":
        cmd.extend(["--sandbox", sandbox])
    if model:
        if not MODEL_ID.fullmatch(model):
            raise CodexError("Invalid model ID")
        cmd.extend(["--model", model])
    if effort:
        if effort not in EFFORTS:
            raise CodexError("Invalid thinking level")
        cmd.extend(["--config", f"model_reasoning_effort={effort}"])
    if thread_id:
        cmd.extend(["resume", thread_id, "-"])
    else:
        cmd.append("-")

    LOG.info("Codex task in %s, resuming=%s", cwd, bool(thread_id))
    if should_cancel and should_cancel():
        raise CodexCancelled("Cancelled before Codex started")
    try:
        # Own process group: stop only this task and the commands it launched.
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        raise CodexError(f"Cannot start Codex CLI: {exc}") from exc

    def stop_child():
        if proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except ProcessLookupError:
                pass
            try:
                proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "posix":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                proc.communicate()

    started = time.monotonic()
    first = True
    try:
        while True:
            if should_cancel and should_cancel():
                raise CodexCancelled("User cancelled the running task")
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise CodexError(
                    f"Codex exceeded {timeout}s; task result uncertain. Check local files before retrying.")
            try:
                # communicate() can safely be retried following TimeoutExpired;
                # it retains prior stdout/stderr and unfinished stdin writes.
                stdout, stderr = proc.communicate(
                    input=prompt if first else None, timeout=min(0.3, remaining))
                break
            except subprocess.TimeoutExpired:
                first = False
        if should_cancel and should_cancel():
            raise CodexCancelled("Cancellation arrived as Codex finished")
    except BaseException:
        stop_child()
        raise

    actual_thread = thread_id
    responses, errors, warnings = [], [], []
    in_tokens = out_tokens = 0
    had_completed = False
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        event_type = event.get("type")
        if event_type == "thread.started":
            actual_thread = event.get("thread_id") or actual_thread
        item = event.get("item") or {}
        if event_type == "item.completed" and item.get("type") == "agent_message":
            responses.append(str(item.get("text") or ""))
        if event_type == "item.completed" and item.get("type") == "error":
            # Codex sometimes emits nonfatal deprecation/compatibility
            # notices with item.type='error' and still completes the turn.
            warnings.append(str(item.get("message", "Codex item warning")))
        if event_type == "turn.failed":
            errors.append(str(event.get("error") or "Turn failed"))
        if event_type == "turn.completed":
            had_completed = True
            usage = event.get("usage") or {}
            in_tokens += int(usage.get("input_tokens") or 0)
            out_tokens += int(usage.get("output_tokens") or 0)
    if proc.returncode != 0:
        errors.append(f"Codex exited {proc.returncode}: {stderr[-500:]}")
    if errors:
        raise CodexError(" | ".join(errors)[-1200:])
    if not had_completed:
        raise CodexError(f"Codex did not finish a turn (exit {proc.returncode}). "
                         f"{' | '.join(warnings)[-400:]} {stderr[-400:]}")
    if warnings:
        LOG.warning("Codex reported %d nonfatal notices: %s", len(warnings), " | ".join(warnings)[-500:])
    return {
        "thread_id": actual_thread,
        "response": responses[-1] if responses else "Codex finished but returned no final text.",
        "input_tokens": in_tokens, "output_tokens": out_tokens,
    }


def last_saved_exchange(codex_bin: str, thread_id: str) -> str | None:
    """Read the latest saved Codex turn without altering/resuming the session.

    Best-effort preview for imported sessions with no bot-local history. Codex
    may not expose paginated history for older sessions; callers display that
    limitation instead of inventing a response.
    """
    if not thread_id:
        return None
    try:
        result = rpc_call(codex_bin, "thread/turns/list", {
            "threadId": thread_id, "limit": 4,
            "sortDirection": "desc", "itemsView": "full",
        }, timeout=20)
    except (OSError, CodexError, ValueError):
        return None
    turns = result.get("data") or []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        user, answers = "", []
        for item in turn.get("items") or []:
            kind = item.get("type")
            if kind == "userMessage":
                user = " ".join(str(c.get("text", "")) for c in item.get("content", [])
                                if isinstance(c, dict) and c.get("type") == "text").strip()
            elif kind == "agentMessage" and item.get("text"):
                answers.append((item.get("phase") or "", str(item["text"])))
        final = next((text for phase, text in reversed(answers) if phase in ("final_answer", "")), "")
        if not final and answers:
            final = answers[-1][1]
        if user or final:
            return f"You: {user or '(not available)'}\nCodex: {final or '(no saved final reply)'}"
    return None
