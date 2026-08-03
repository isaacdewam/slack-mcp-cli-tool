# Slack self-DM → Claude Code runner

`slack_claude_runner.py` watches your Slack **Message yourself** DM for messages starting with `!run:`. It runs the rest as a Claude Code task, posts brief progress in the trigger thread, and replies with `Done:` or `Error:`.

It uses Slack's official hosted MCP server. It does not read browser tokens, store Slack tokens, create a bot user, or call the Slack Web API directly.

## Setup

Requirements:

- Python 3.10+
- Claude Code CLI
- Slack MCP access approved by your workspace admin

Recommended Slack setup:

```bash
claude plugin install slack@claude-plugins-official
claude mcp login plugin:slack:slack
```

If you already connected Slack, you can verify it with:

```bash
claude mcp list
```

Make the runner executable:

```bash
chmod +x slack_claude_runner.py
```

## Usage

Dry-run a single poll:

```bash
/absolute/path/to/slack_claude_runner.py --once --dry-run --verbose
```

Run continuously:

```bash
/absolute/path/to/slack_claude_runner.py \
  --workdir /absolute/path/to/repository \
  --interval 30
```

In Slack, send yourself something like:

```text
!run: Add unit tests for the date parser and run them
```

Useful flags:

```text
--once                 Poll once, for cron
--interval 60          Poll every 60 seconds
--progress-interval 30 Batch progress posts
--max-budget-usd 5     Cap each task run
--task-model sonnet    Override the task model
--helper-model haiku   Model for Slack helper calls
--permission-mode auto Hands-off Claude safety mode
--state-file PATH      Put state somewhere else
```

Run `./slack_claude_runner.py --help` for the full list.

## Tests

```bash
python3 -m unittest discover -s tests
```

## Background runs

Cron example:

```cron
* * * * * cd /absolute/path/to/repository && /usr/bin/python3 /absolute/path/to/slack_claude_runner.py --once --claude-bin /absolute/path/to/claude >> /absolute/path/to/slack-claude-runner.log 2>&1
```

systemd example:

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

## Notes

- State lives in `~/.local/state/slack-claude-runner/state.json` by default and tracks handled messages plus one Claude session per Slack thread.
- A task only starts after the acknowledgement message posts successfully.
- Progress posts are throttled, and the runner retries on Slack tool failures.
- Keep it pointed at repos and machines you trust. Anyone who can message your Slack self-DM can ask Claude to act with your local permissions.

References:

- [Slack: Connect to Claude](https://docs.slack.dev/ai/slack-mcp-server/connect-to-claude/)
- [Slack MCP server overview and rate limits](https://docs.slack.dev/ai/slack-mcp-server/)
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-usage)
