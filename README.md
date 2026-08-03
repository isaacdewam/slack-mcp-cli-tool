# Slack self-DM → Claude Code runner

`slack_claude_runner.py` watches your own Slack **Message yourself** DM for
messages beginning with `!run:`. It runs the remainder as a Claude Code task,
posts throttled tool/file progress in the trigger's Slack thread, and posts a
final `Done: ...` or `Error: ...` reply.

The runner uses Slack's official hosted MCP server. It does not read browser
tokens, store Slack tokens, create a bot user, or call the Slack Web API
directly.

## 1. Prerequisites and Slack MCP setup

You need:

- Python 3.10 or newer (no third-party Python packages)
- A working `claude` CLI
- Slack MCP access approved by your workspace admin

Slack's current official Claude Code setup is the Slack plugin. It configures
`https://mcp.slack.com/mcp` with Slack's registered Claude client identity:

```bash
claude plugin install slack@claude-plugins-official
claude mcp login plugin:slack:slack
```

If it is already connected and authenticated, the login command is unnecessary.
Verify the connection with `claude mcp list`, or start Claude Code and run
`/mcp`:

```bash
claude mcp list
```

The equivalent manual MCP registration is:

```bash
claude mcp add --transport http --scope user \
  --client-id 1601185624273.8899143856786 \
  --callback-port 3118 \
  slack https://mcp.slack.com/mcp
claude mcp login slack
```

The client ID and callback port above are the values published in Slack's
official Claude integration configuration. Prefer the plugin so Slack can keep
that configuration current.

Make the runner executable:

```bash
chmod +x slack_claude_runner.py
```

## 2. Test it

From the repository Claude should work in, first inspect pending commands
without executing them:

```bash
/absolute/path/to/slack_claude_runner.py --once --dry-run --verbose
```

Then start the 30-second foreground polling loop:

```bash
/absolute/path/to/slack_claude_runner.py \
  --workdir /absolute/path/to/repository \
  --interval 30
```

In Slack, send yourself a message such as:

```text
!run: Add unit tests for the date parser and run them
```

The script acknowledges the command before starting it. Status and the final
result are replies in that message's thread.

State defaults to
`~/.local/state/slack-claude-runner/state.json` with mode `0600`. It records
handled Slack message timestamps and maps each Slack thread to its Claude
session ID. A later `!run:` reply in the same Slack thread is therefore run with
`claude --resume <session_id>`.

Useful options:

```text
--once                    Poll once, for cron
--interval 60             Poll every 60 seconds
--progress-interval 30    Batch progress posts for at least 30 seconds
--max-budget-usd 5        Cap each task run
--task-model sonnet       Override the task model
--helper-model haiku      Model used for small Slack MCP calls
--permission-mode auto    Hands-off mode with Claude safety classification
--state-file PATH         Put state somewhere else
```

Run `./slack_claude_runner.py --help` for the full list.

## 3. Run it in the background

### Cron

Use `--once`; cron supplies the loop. A one-minute interval is a practical
starting point. Use absolute paths because cron usually has a minimal `PATH`:

```cron
* * * * * cd /absolute/path/to/repository && /usr/bin/python3 /absolute/path/to/slack_claude_runner.py --once --claude-bin /absolute/path/to/claude >> /absolute/path/to/slack-claude-runner.log 2>&1
```

The state-file lock makes overlapping cron invocations exit instead of running
a command twice.

### systemd

Create `~/.config/systemd/user/slack-claude-runner.service`:

```ini
[Unit]
Description=Slack self-DM Claude Code runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/absolute/path/to/repository
ExecStart=/usr/bin/python3 /absolute/path/to/slack_claude_runner.py --interval 60 --claude-bin /absolute/path/to/claude
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
```

Enable and follow its logs:

```bash
systemctl --user daemon-reload
systemctl --user enable --now slack-claude-runner
journalctl --user -u slack-claude-runner -f
```

## Behavior and failure handling

- **Slack auth failure:** the process logs an explicit `AUTH ERROR` and tells
  you to run `claude mcp login plugin:slack:slack` (or `claude mcp login slack`
  for a manually registered server). In loop mode it retries on the next poll;
  `--once` exits with status 3.
- **Slack connection/tool failure:** the operation is logged and retried on the
  next poll. A task does not begin unless its acknowledgement was posted.
- **Claude task failure:** the script posts `Error: Claude Code run failed: ...`
  in the trigger thread and records that message as handled so it cannot loop.
- **Restart or duplicate process:** state is written atomically and guarded by
  a lock. A message marked `processing` remains handled after a crash to avoid
  accidentally executing the same remote command twice; send a new `!run:`
  reply to retry.
- **Session continuity:** each Slack thread has one persisted Claude session.
  Commands in different threads start separate sessions.

## Polling, cost, and rate limits

Every poll invokes a small `claude -p` helper that asks Slack MCP to read the
self-DM. Every Slack status post also uses a short helper call because the
design deliberately has no Slack token outside MCP. Those calls consume Claude
usage in addition to the actual task.

Slack MCP applies Slack Web API rate limits per tool. Read-channel and
read-thread actions are currently Tier 3 (50+ per minute), while message search
and send-message have special limits. A 30-second interval is convenient for
testing; 60–120 seconds is kinder for continuous use. Progress posts are
batched to at most one batch per `--progress-interval` while tools are active.
On rate-limit errors, increase both intervals.

## Security notes

This is intentionally a remote code-execution bridge. Anyone who can write as
you in your Slack self-DM can ask Claude Code to act with the OS permissions of
the service account.

- Run it only against repositories and working directories you trust.
- Protect your Slack account with MFA and protect the machine running it.
- The default task permission mode is Claude Code `auto`, which performs
  background safety checks and blocks destructive or suspicious actions.
- Keep your normal Claude permission deny rules. Do not run with bypass
  permissions on a personal workstation.
- Use `--max-budget-usd` when the account is billed through the API.
- Review the local service logs; if Slack is unavailable, the script cannot
  report that outage back through Slack itself.

Official references:

- [Slack: Connect to Claude](https://docs.slack.dev/ai/slack-mcp-server/connect-to-claude/)
- [Slack MCP server overview and rate limits](https://docs.slack.dev/ai/slack-mcp-server/)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage)
- [Claude Code permission modes](https://code.claude.com/docs/en/permission-modes)
