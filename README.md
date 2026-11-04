# WhatsApp ↔ Codex: multiple named chats

This is a personal WhatsApp bot bridging Meta WhatsApp Cloud API to **local Codex CLI** on Ubuntu. Each WhatsApp sender has multiple named Codex threads, stored in SQLite. A single WhatsApp conversation uses commands to switch the current thread. The CLI **inherits the sandbox and approval settings of the Ubuntu user running the bot** (including managed and project settings). The bot itself does not force read-only mode. Codex can edit files and run tests when those settings allow it. Non-interactive sessions cannot answer arbitrary interactive approval prompts.

It does **not** attach to a running tmux TUI. It starts/resumes persistent local Codex CLI sessions by thread ID. It is not ChatGPT Remote Control, and cannot override a workspace security restriction. Do not expose work code or devices if company policy forbids it.

## Chat commands

```
/new research        # create and select new chat
/new debugging       # another independent Codex chat
/list                # show named chats; * means active
/use research        # switch back, preserving context
/rename research docs
/status
/reset               # forget active thread in bot (old history remains on disk)
/delete debugging    # delete bot mapping, not local Codex history
/help
```

Names use ASCII letters, numbers, `_` or `-`, start with a letter, and are max 32 characters. All messages without slash commands go to the current chat. The initial default chat is `main`.

## Prerequisites

- Ubuntu host that can reach the internet and stays running
- Python 3.10+ and `codex` installed and authenticated (`codex login`), under the **same Ubuntu account** running the bot
- Meta Developer account, WhatsApp Business Platform / Cloud API test or production number, app secret, phone number ID, access token, and an approved receiving WhatsApp number
- Public **HTTPS** webhook forwarding to localhost port 8000 (for example, an approved tunnel/reverse proxy); configure its path as `/webhook`
- Meta application webhook subscription to the `messages` field

## Install

```bash
unzip whatsapp_codex_native_permissions.zip
cd whatsapp_codex_native_permissions
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
codex --version
codex login status
python bot.py
```

In `.env`, fill in Meta credentials and your personal number (country code and digits, no `+`). Set `CODEX_PROJECT_DIR` to the authorized folder on `nayaka5`. `META_APP_SECRET` is for verifying `X-Hub-Signature-256`; the verify token is a separate random string, which must match your Meta webhook configuration.

The Flask app listens on `127.0.0.1:8000`. Your HTTPS proxy/tunnel must forward to it, e.g. webhook callback URL `https://your-approved-domain.example/webhook`. Keep the credentials secret and restrict who can reach the proxy. For production deployments, use a supported application server and durable work queue instead of the included single-process development runner.

## Behaviour and limitations

- One local worker processes jobs serially; each named chat preserves its own CLI thread context. Chat history survives bot restarts if `bot.sqlite3` is preserved.
- Sessions all run with `CODEX_PROJECT_DIR` as their working directory. Different chats are not separate repositories or sandboxes. For parallel repositories, deploy separately or extend the configuration with a per-chat allowlist.
- New threads are created when the first message arrives. The bot saves `thread.started` IDs and uses `codex exec --json resume SESSION_ID -` on follow-up turns.
- By default, WhatsApp Cloud API customer-service replies have a 24-hour window after an inbound message; outbound templates may be required outside it. Meta may charge message fees.
- Incoming webhook signatures and an explicit sender allowlist are enforced. Do **not** set broad access. The bot keeps conversation metadata in an unencrypted local SQLite DB and Codex stores its own conversation history locally.
- The bot **does not override** Codex sandbox or approval settings. For `on-request` approvals, operations that need interactive approval may fail or stop; the bot does not grant itself more access. Never set `danger-full-access` or `approval_policy = "never"` just to make WhatsApp automation work. If appropriate, use workspace-write with an approved project directory.
- The sample uses an in-memory job queue. Requests in flight can be lost during a process restart, and WhatsApp delivery failures require reviewing logs.
- WhatsApp messages may be handled by Meta's systems. Avoid sensitive credentials, secrets, or employer-confidential source material without permission.
- This does not attach to a live tmux TUI, share the IDE extension chat automatically, or let WhatsApp act as a replacement for VS Code. It can create/edit code in the configured project when Codex permissions permit it. It is a text-command interface to Codex CLI.

Reference: https://learn.chatgpt.com/docs/cli/reference

## Using your existing Codex permissions

The bot intentionally calls `codex exec --json` (and `codex exec --json resume ...`) **without** `--sandbox`, `--ask-for-approval`, `--yolo`, or `--skip-git-repo-check`. It inherits the effective configuration from the Codex CLI launched as the bot's Linux user, including `~/.codex/config.toml`, trusted project configuration, and managed policy.

Check the account and settings first:

```bash
whoami
codex login status
cat ~/.codex/config.toml
cd /path/to/your/project
codex exec 'Inspect the repository, do not change files, and report the configured execution context.'
```

If your interactive Codex session was started with **command-line permission overrides**, those transient overrides do not automatically carry into this independent process. Configure the **approved** equivalent in Codex config if you actually want it to apply to the bot. Never assume the bot has the exact same effective permissions until you test a harmless file edit in a dedicated test repo.

For example, send `/new test` and then `Create a file called whatsapp_codex_test.txt containing hello`. Verify the file appears where expected. Delete it afterward. If Codex reports an approval denial, leave the boundary in place and use the local terminal for that task or implement an explicit approval flow after review.

**Security**: WhatsApp messages can trigger local code edits and shell commands. Restrict the sender list, keep Meta credentials secure, restrict the workspace to an approved repo, and do not use this setup to circumvent an organization's disabled remote-control setting.
