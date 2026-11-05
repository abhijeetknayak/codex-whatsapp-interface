# WhatsApp ↔ Codex

A locally hosted WhatsApp Cloud API bridge for an already-authorized Codex CLI installation. Developed for Ubuntu (`nayaka5`), Python 3.11+, and WhatsApp Cloud API webhooks. It does **not** use OpenAI API keys: it launches the locally authenticated Codex CLI under the same Linux account, inheriting its effective Codex settings and sandbox restrictions.

## Features

- Preserves the existing bot's commands: `/new`, `/list`, `/use`, `/rename`, `/delete`, `/reset`, `/status`, `/help`, `/server`, `/import`, `/model`.
- Adds `/thinking`: numbered reasoning-effort menu **from the selected model's supported options**. `/model` also uses the Codex app-server's live catalog, with a local cache fallback. Selections persist per chat in SQLite and are applied to `codex exec` and `codex exec resume`.
- Adds `/usage`: reads `account/rateLimits/read` from local Codex app-server and prints *actual* 5-hour and weekly window data when provided, including percentage and reset. Other windows are shown if reported. Unsupported/unavailable windows are clearly marked, and bot-local token totals are separate.
- Adds `/jobs` and `/history [N]`, plus a `last request + last answer + status` preview on `/use`.
- Asynchronous jobs: switching chats does **not** interrupt Codex tasks. Each completion is tagged `[chatname]`. Different projects can run concurrently; **the same project is serialized** to avoid conflicting edits. A later message in the same chat waits for its predecessor to finish.
- `/server [PAGE]` gets genuine titles from Codex app-server (`thread/list`), filtering out subagent/review threads. `/import NAME NUMBER` imports the chosen session using a saved numbered snapshot; its project path must be under `CODEX_ALLOWED_ROOTS`. `/use` can display a saved Codex last turn for an imported session, where the CLI supports history pagination.
- SQLite persistence: inbound webhook deduplication, durable job queue, saved messages, replies and settings, plus an outgoing queue. Started jobs are marked **interrupted** after a crash, not automatically re-executed (important for code edits).
- Keeps signed webhook verification, allowlisted phone numbers, local-only bind, and **no sandbox/approval bypass flags**.

### WhatsApp commands

| Command | Action |
|---|---|
| `/help` | All commands |
| `/new robotics` | Create and select a named chat |
| `/chats` or `/list` | List bot chats |
| `/use robotics` | Switch + display latest prompt, answer, and status |
| `/rename old new` | Rename chat locally |
| `/delete name` | Delete local bot chat mapping + messages, **not** the actual Codex session |
| `/reset` | Start a new Codex session in active chat (old history remains in Codex) |
| `/model` then `1` | Set model (use `0` for default); `/model 2` while menu open also works |
| `/thinking` then `2` | Set supported reasoning effort (use `0` for default) |
| `/usage` | 5-hour + weekly quota where available; local token totals separately |
| `/server` or `/server 2` | Browse Codex CLI/VS Code/exec sessions with readable names |
| `/import legacy 7` | Import session number 7 from last `/server` snapshot |
| `/jobs` | Running and queued tasks |
| `/history 3` | Last three requests and Codex replies |
| `/status` | Chat, model, thinking, directory, jobs |
| `/project` or `/project /approved/project` | Show/set chat's working directory inside configured roots |

**Example workflow:** `/new robotics` → `Fix sensor calibration, run tests` → `/use planning` → `Explain the trajectory code` → `/use robotics` to see the latest exchange. If both projects point to the same directory, the second *task waits* while commands and chat switching still work.

### First tests from your phone

1. `/help` — verifies outgoing Meta messaging.
2. `/chats` — verifies database migration.
3. `/model` then a number, `/thinking` then a number — verify the menu works.
4. `/usage` — checks whether your Codex login exposes 5-hour and weekly quotas.
5. `/server` and `/server 2` — verify familiar session titles appear.
6. `/new test` then `Tell me the current directory; do not edit any files.` — verify Codex execution.
7. Switch `/use main` while a task in another project is running, then switch back.

If model-list or usage reads fail, check that `codex app-server` works under the same Linux user and that Codex is signed in with a supported ChatGPT account. These features depend on your installed CLI and account. The bot never invents usage limits.

## Optional: stable production service

After you confirm v2 works, replace the temporary `trycloudflare.com` Quick Tunnel with a named Cloudflare Tunnel on your domain, and run the bot using `systemd` instead of tmux. A template is included in `whatsapp-codex-v2.service.example`. **Stop the tmux bot before enabling systemd.** The example uses Gunicorn with *one* worker process (multiple workers would start duplicate queue processors); the bot itself handles concurrent Codex tasks using threads.

```bash
sudo cp whatsapp-codex-v2.service.example /etc/systemd/system/whatsapp-codex.service
sudo nano /etc/systemd/system/whatsapp-codex.service  # verify paths
sudo systemctl daemon-reload
sudo systemctl enable --now whatsapp-codex
journalctl -u whatsapp-codex -f
```

Gunicorn should bind `127.0.0.1:8000`, not all network interfaces. Your Cloudflare connector forwards only to the localhost service. `systemd` should run the service as the same Linux user that owns the Codex sessions and `.env`.

## Behavior, safety, and limits

- `/use` sends a **new WhatsApp message** with the last exchange. WhatsApp cannot erase or replace the visible message history in its own UI.
- Model and reasoning preferences affect **future turns**. Supported effort levels come from the currently reported model catalog. If a session is resumed under a different model, Codex may warn about the change.
- The bot does not attach to a **live tmux/interactive Codex session**. It resumes persisted session histories. Don't send simultaneous turns to a session from two different clients.
- Non-interactive approval prompts are **not handled** by this bot; it does not add `--yolo` or bypass sandbox checks. Local and workspace policies still apply.
- `MAX_CODEX_WORKERS` controls concurrent tasks across **different** project directories. A repo is serialized irrespective of chat name. No auto-retry of interrupted code edits: inspect files before retrying a failed task.
- Meta replies may fail if the WhatsApp system-user token expires, loses asset access, or is outside WhatsApp's permitted conversation window. Outgoing API 4xx errors are logged and not retried indefinitely. Network-ambiguous sends are marked `uncertain` to avoid automatic duplicate messages.
- Never place Meta access tokens, App Secret, Codex authentication files, or your personal phone numbers in screenshots or pasted logs. `.env` stays local with mode `0600`.
- Restrict `CODEX_ALLOWED_ROOTS` to directories you actually want accessible, and follow your organization’s policies before exposing a company-managed machine to a WhatsApp bridge.

## Tests

```bash
cd codex-whatsapp-interface
PYTHONPATH=.:tests python -m unittest discover -s tests -v
```

Tests use simulated Meta payloads and a **fake Codex executable**, so they don't contact your account or modify a real repository. The test suite has a tiny Flask shim for environments where Flask cannot be installed; production always uses the real Flask dependency.

## Reference documentation

- [Codex CLI command reference](https://learn.chatgpt.com/docs/cli/reference)
- [Codex app-server: `model/list`, `thread/list`, `account/rateLimits/read`](https://learn.chatgpt.com/docs/app-server)
- [WhatsApp Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api/)
