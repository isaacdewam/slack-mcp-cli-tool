#!/usr/bin/env python3
"""Poll a Slack self-DM for !run: commands and execute them with Claude Code."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


LOG = logging.getLogger("slack-claude-runner")
COMMAND_PREFIX = "!run:"
DEFAULT_INTERVAL_SECONDS = 30
DEFAULT_PROGRESS_INTERVAL_SECONDS = 15
DEFAULT_HELPER_TIMEOUT_SECONDS = 120
MAX_SLACK_MESSAGE_CHARS = 3_500
MAX_RESULT_CHARS = 3_200
MAX_STATE_MESSAGES = 5_000
SLACK_ALLOWED_TOOLS = "mcp__slack__*,mcp__plugin_slack_slack__*"

DISCOVERY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "self_dm_channel_id": {"type": "string"},
        "commands": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "channel_id": {"type": "string"},
                    "message_ts": {"type": "string"},
                    "thread_ts": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["channel_id", "message_ts", "thread_ts", "text"],
            },
        },
    },
    "required": ["self_dm_channel_id", "commands"],
}

AUTH_ERROR_RE = re.compile(
    r"(?:\b401\b|\b403\b|unauthori[sz]ed|not authenticated|authentication "
    r"(?:failed|required|error)|oauth|log ?in.*slack|slack.*log ?in)",
    re.IGNORECASE,
)


class RunnerError(RuntimeError):
    """Base error for a recoverable runner failure."""


class SlackMCPError(RunnerError):
    """Slack MCP could not complete an operation."""


class SlackAuthError(SlackMCPError):
    """Slack MCP needs authentication."""


class ClaudeRunError(RunnerError):
    """A Claude Code subprocess failed."""


@dataclass(frozen=True)
class Trigger:
    channel_id: str
    message_ts: str
    thread_ts: str
    text: str

    @property
    def task(self) -> str:
        return self.text.strip()[len(COMMAND_PREFIX) :].strip()

    @property
    def message_key(self) -> str:
        return f"{self.channel_id}:{self.message_ts}"

    @property
    def thread_key(self) -> str:
        return f"{self.channel_id}:{self.thread_ts}"


@dataclass
class HelperResult:
    events: list[dict[str, Any]]
    result_event: dict[str, Any] | None
    returncode: int
    diagnostics: str
    saw_slack_tool: bool


@dataclass
class TaskResult:
    ok: bool
    summary: str
    session_id: str | None
    diagnostics: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state_path() -> Path:
    configured = os.environ.get("SLACK_CLAUDE_STATE_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "slack-claude-runner" / "state.json"


def compact_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def slack_text(value: Any, limit: int = MAX_SLACK_MESSAGE_CHARS) -> str:
    text = str(value or "").replace("\x00", "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def timestamp_sort_key(value: str) -> tuple[int, int]:
    seconds, dot, fraction = value.partition(".")
    try:
        return int(seconds), int(fraction or "0") if dot else 0
    except ValueError:
        return 0, 0


def parse_json_lines(output: str) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            diagnostics.append(line)
            continue
        if isinstance(value, dict):
            events.append(value)
        else:
            diagnostics.append(line)
    return events, diagnostics


def message_content(event: dict[str, Any]) -> Iterable[dict[str, Any]]:
    message = event.get("message")
    if not isinstance(message, dict):
        return ()
    content = message.get("content")
    if not isinstance(content, list):
        return ()
    return (block for block in content if isinstance(block, dict))


def event_uses_slack(event: dict[str, Any]) -> bool:
    for block in message_content(event):
        if block.get("type") not in {"tool_use", "server_tool_use"}:
            continue
        name = str(block.get("name", "")).lower()
        if "slack" in name:
            return True
    return False


def event_error_text(event: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in ("error", "errors", "result"):
        value = event.get(field)
        if isinstance(value, list):
            errors.extend(str(item) for item in value)
        elif value and (field != "result" or event.get("is_error")):
            errors.append(str(value))
    for block in message_content(event):
        if block.get("type") == "tool_result" and block.get("is_error"):
            errors.append(str(block.get("content", "tool call failed")))
    return errors


def result_is_success(event: dict[str, Any] | None) -> bool:
    if not event:
        return False
    if event.get("type") != "result" or event.get("is_error"):
        return False
    return event.get("subtype", "success") == "success"


def terminate_process(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


class StateStore:
    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self.lock_path = Path(f"{self.path}.lock")
        self._lock_file: Any = None
        self.data: dict[str, Any] = {}

    def acquire_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunnerError(
                f"Another runner is using state file {self.path}"
            ) from exc

    def load(self) -> None:
        if not self.path.exists():
            self.data = {
                "version": 1,
                "self_dm_channel_id": "",
                "messages": {},
                "threads": {},
            }
            return
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RunnerError(f"Cannot read state file {self.path}: {exc}") from exc
        if not isinstance(loaded, dict):
            raise RunnerError(f"State file {self.path} is not a JSON object")
        loaded.setdefault("version", 1)
        loaded.setdefault("self_dm_channel_id", "")
        loaded.setdefault("messages", {})
        loaded.setdefault("threads", {})
        if not isinstance(loaded["messages"], dict) or not isinstance(
            loaded["threads"], dict
        ):
            raise RunnerError(f"State file {self.path} has an invalid shape")
        self.data = loaded

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prune_messages()
        temp_path = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        payload = json.dumps(self.data, indent=2, sort_keys=True) + "\n"
        try:
            temp_path.write_text(payload, encoding="utf-8")
            temp_path.chmod(0o600)
            os.replace(temp_path, self.path)
        finally:
            if temp_path.exists():
                temp_path.unlink()

    def _prune_messages(self) -> None:
        messages = self.data["messages"]
        if len(messages) <= MAX_STATE_MESSAGES:
            return
        ordered = sorted(
            messages.items(),
            key=lambda item: str(item[1].get("updated_at", "")),
        )
        for key, _ in ordered[: len(messages) - MAX_STATE_MESSAGES]:
            messages.pop(key, None)

    def is_handled(self, trigger: Trigger) -> bool:
        return trigger.message_key in self.data["messages"]

    def mark_message(self, trigger: Trigger, status: str, detail: str = "") -> None:
        self.data["messages"][trigger.message_key] = {
            "status": status,
            "thread_key": trigger.thread_key,
            "task_sha256": hashlib.sha256(
                trigger.task.encode("utf-8")
            ).hexdigest(),
            "detail": compact_text(detail, 500),
            "updated_at": utc_now(),
        }
        self.save()

    def session_for(self, trigger: Trigger) -> str | None:
        entry = self.data["threads"].get(trigger.thread_key)
        if not isinstance(entry, dict):
            return None
        session_id = entry.get("session_id")
        return str(session_id) if session_id else None

    def set_session(self, trigger: Trigger, session_id: str) -> None:
        self.data["threads"][trigger.thread_key] = {
            "session_id": session_id,
            "updated_at": utc_now(),
        }
        self.save()

    def set_self_dm_channel(self, channel_id: str) -> None:
        if channel_id and channel_id != self.data.get("self_dm_channel_id"):
            self.data["self_dm_channel_id"] = channel_id
            self.save()


class ClaudeClient:
    def __init__(
        self,
        claude_bin: str,
        helper_model: str | None,
        helper_timeout: int,
        permission_mode: str,
        task_model: str | None,
        max_budget_usd: float | None,
    ):
        self.claude_bin = claude_bin
        self.helper_model = helper_model
        self.helper_timeout = helper_timeout
        self.permission_mode = permission_mode
        self.task_model = task_model
        self.max_budget_usd = max_budget_usd

    def _helper_command(
        self, prompt: str, schema: dict[str, Any] | None = None
    ) -> list[str]:
        command = [
            self.claude_bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--tools",
            "ToolSearch",
            "--allowedTools",
            SLACK_ALLOWED_TOOLS,
        ]
        if self.helper_model:
            command.extend(["--model", self.helper_model])
        if schema is not None:
            command.extend(
                [
                    "--json-schema",
                    json.dumps(schema, separators=(",", ":")),
                ]
            )
        command.append(prompt)
        return command

    def run_helper(
        self, prompt: str, schema: dict[str, Any] | None = None
    ) -> HelperResult:
        command = self._helper_command(prompt, schema)
        LOG.debug("Starting Claude helper process")
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=self.helper_timeout)
        except subprocess.TimeoutExpired as exc:
            terminate_process(proc)
            raise SlackMCPError(
                f"Claude helper timed out after {self.helper_timeout}s"
            ) from exc
        events, non_json = parse_json_lines(stdout)
        result_event = next(
            (event for event in reversed(events) if event.get("type") == "result"),
            None,
        )
        all_errors: list[str] = []
        for event in events:
            all_errors.extend(event_error_text(event))
        diagnostics = "\n".join(
            part
            for part in (
                stderr.strip(),
                "\n".join(non_json),
                "\n".join(all_errors),
            )
            if part
        )
        return HelperResult(
            events=events,
            result_event=result_event,
            returncode=proc.returncode,
            diagnostics=diagnostics,
            saw_slack_tool=any(event_uses_slack(event) for event in events),
        )

    def discover(
        self, cached_channel_id: str, max_commands: int
    ) -> tuple[str, list[Trigger]]:
        channel_hint = (
            f"The previously verified self-DM channel ID is {cached_channel_id!r}. "
            "Verify and use it."
            if cached_channel_id
            else "Locate the authenticated user's Slack 'Message yourself' DM first."
        )
        prompt = f"""
Use only the official Slack MCP tools. {channel_hint}

Find the {max_commands} most recent messages in that self-DM whose trimmed text
starts exactly with {COMMAND_PREFIX!r}. Include top-level messages and thread
replies. Do not inspect or return commands from any other DM or channel.

Return:
- self_dm_channel_id: the verified Slack conversation ID
- commands: newest relevant messages, each with channel_id, message_ts, text,
  and thread_ts. For a top-level message, thread_ts must equal message_ts. For a
  reply, thread_ts must be the root message timestamp.

Keep Slack timestamps as strings. Return an empty commands list if there are no
matches. Do not send, draft, react to, or modify any Slack message.
""".strip()
        response = self.run_helper(prompt, DISCOVERY_SCHEMA)
        self._require_slack_success(response, "read the Slack self-DM")
        structured = (response.result_event or {}).get("structured_output")
        if not isinstance(structured, dict):
            raise SlackMCPError(
                "Claude completed Slack discovery without structured output"
            )
        channel_id = str(structured.get("self_dm_channel_id", "")).strip()
        raw_commands = structured.get("commands")
        if not channel_id:
            raise SlackMCPError("Slack discovery did not identify the self-DM channel")
        if not isinstance(raw_commands, list):
            raise SlackMCPError("Slack discovery returned an invalid commands list")

        triggers: list[Trigger] = []
        for item in raw_commands:
            if not isinstance(item, dict):
                continue
            trigger = Trigger(
                channel_id=str(item.get("channel_id", "")).strip(),
                message_ts=str(item.get("message_ts", "")).strip(),
                thread_ts=str(item.get("thread_ts", "")).strip(),
                text=str(item.get("text", "")),
            )
            if (
                trigger.channel_id != channel_id
                or not trigger.message_ts
                or not trigger.thread_ts
                or not trigger.text.strip().startswith(COMMAND_PREFIX)
                or not trigger.task
            ):
                LOG.warning("Ignoring malformed or out-of-scope Slack command")
                continue
            triggers.append(trigger)
        triggers.sort(key=lambda trigger: timestamp_sort_key(trigger.message_ts))
        return channel_id, triggers

    def post_thread(self, trigger: Trigger, text: str) -> None:
        message = slack_text(text)
        prompt = f"""
Use the official Slack MCP send-message tool to send exactly one message.

Destination channel ID: {trigger.channel_id}
Reply in thread timestamp: {trigger.thread_ts}
Message text:
---BEGIN MESSAGE---
{message}
---END MESSAGE---

Send it as a thread reply. Do not send anywhere else, do not draft it, and do
not add any other text to the Slack message. After the tool succeeds, respond
with only POSTED.
""".strip()
        last_response: HelperResult | None = None
        for attempt in range(2):
            response = self.run_helper(prompt)
            last_response = response
            try:
                self._require_slack_success(response, "post a Slack thread reply")
                return
            except SlackAuthError:
                raise
            except SlackMCPError:
                if attempt == 0:
                    time.sleep(2)
                    continue
                raise
        self._require_slack_success(last_response, "post a Slack thread reply")

    @staticmethod
    def _require_slack_success(
        response: HelperResult | None, operation: str
    ) -> None:
        if response is None:
            raise SlackMCPError(f"Could not {operation}: no Claude response")
        result_text = str((response.result_event or {}).get("result", ""))
        diagnostics = "\n".join(
            part for part in (response.diagnostics, result_text) if part
        )
        if AUTH_ERROR_RE.search(diagnostics):
            raise SlackAuthError(
                f"Could not {operation}: Slack MCP authentication failed. "
                "Run `claude mcp login plugin:slack:slack` for the official "
                "plugin (or `claude mcp login slack` for a manual server)."
            )
        if (
            response.returncode != 0
            or not result_is_success(response.result_event)
            or not response.saw_slack_tool
        ):
            detail = compact_text(
                diagnostics or "Slack tool was not called successfully",
                900,
            )
            raise SlackMCPError(f"Could not {operation}: {detail}")

    def run_task(
        self,
        trigger: Trigger,
        resume_session_id: str | None,
        on_session: Callable[[str], None],
        on_progress: Callable[[str], None],
    ) -> TaskResult:
        prompt = f"""
This task was requested by the authenticated user from their private Slack
self-DM. Work in the current directory and complete the task. Follow all normal
Claude Code safety and permission rules. Do not use Slack for status reporting;
the wrapper handles that. End with a concise result summary suitable for the
requesting user.

Actual task:
{trigger.task}
""".strip()
        command = [
            self.claude_bin,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            self.permission_mode,
        ]
        if resume_session_id:
            command.extend(["--resume", resume_session_id])
        if self.task_model:
            command.extend(["--model", self.task_model])
        if self.max_budget_usd is not None:
            command.extend(["--max-budget-usd", str(self.max_budget_usd)])
        command.append(prompt)

        LOG.info(
            "%s Claude session for Slack thread %s",
            "Resuming" if resume_session_id else "Starting",
            trigger.thread_ts,
        )
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
        stderr_lines: list[str] = []

        def drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                clean = line.rstrip()
                if clean:
                    stderr_lines.append(clean)
                    LOG.debug("claude stderr: %s", clean)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        session_id = resume_session_id
        result_event: dict[str, Any] | None = None
        parse_diagnostics: list[str] = []
        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    parse_diagnostics.append(line)
                    LOG.debug("Non-JSON Claude output: %s", line)
                    continue
                if not isinstance(event, dict):
                    continue
                event_session_id = event.get("session_id")
                if event_session_id and str(event_session_id) != session_id:
                    session_id = str(event_session_id)
                    on_session(session_id)
                if event.get("type") == "assistant":
                    for block in message_content(event):
                        if block.get("type") == "tool_use":
                            progress = describe_tool_use(block)
                            if progress:
                                on_progress(progress)
                if event.get("type") == "result":
                    result_event = event
            returncode = proc.wait()
            stderr_thread.join(timeout=2)
        except BaseException:
            terminate_process(proc)
            stderr_thread.join(timeout=2)
            raise

        diagnostics_parts = stderr_lines + parse_diagnostics
        if result_event:
            diagnostics_parts.extend(event_error_text(result_event))
        diagnostics = compact_text("\n".join(diagnostics_parts), 1_500)
        ok = returncode == 0 and result_is_success(result_event)
        if ok:
            summary = compact_text(
                (result_event or {}).get("result") or "Task completed.",
                MAX_RESULT_CHARS,
            )
        else:
            summary = compact_text(
                diagnostics
                or (result_event or {}).get("result")
                or f"Claude Code exited with status {returncode}",
                MAX_RESULT_CHARS,
            )
        return TaskResult(
            ok=ok,
            summary=summary,
            session_id=session_id,
            diagnostics=diagnostics,
        )


def describe_tool_use(block: dict[str, Any]) -> str | None:
    name = str(block.get("name", "tool"))
    tool_input = block.get("input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    short_name = name.rsplit("__", 1)[-1]
    path = next(
        (
            str(tool_input[key])
            for key in ("file_path", "path", "notebook_path")
            if tool_input.get(key)
        ),
        "",
    )
    if path:
        return f"Using {short_name} on {compact_text(path, 220)}"
    if short_name.lower() in {"bash", "shell", "powershell"}:
        return "Running a shell command"
    if short_name.lower() in {"task", "agent"}:
        description = tool_input.get("description")
        return (
            f"Delegating: {compact_text(description, 220)}"
            if description
            else "Delegating a subtask"
        )
    return f"Using {compact_text(short_name, 120)}"


class AsyncPoster:
    def __init__(self, client: ClaudeClient, trigger: Trigger):
        self.client = client
        self.trigger = trigger
        self.queue: queue.Queue[str | None] = queue.Queue()
        self.errors: list[str] = []
        self.auth_failed = False
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def post(self, text: str) -> None:
        self.queue.put(text)

    def _worker(self) -> None:
        while True:
            text = self.queue.get()
            try:
                if text is None:
                    return
                if self.auth_failed:
                    continue
                try:
                    self.client.post_thread(self.trigger, text)
                except SlackAuthError as exc:
                    self.auth_failed = True
                    self.errors.append(str(exc))
                    LOG.error("AUTH ERROR: %s", exc)
                except SlackMCPError as exc:
                    self.errors.append(str(exc))
                    LOG.error("Slack status post failed: %s", exc)
            finally:
                self.queue.task_done()

    def finish(self) -> list[str]:
        self.queue.join()
        self.queue.put(None)
        self.queue.join()
        self.thread.join(timeout=2)
        return list(self.errors)


class Runner:
    def __init__(
        self,
        client: ClaudeClient,
        state: StateStore,
        interval: int,
        progress_interval: int,
        max_commands: int,
        once: bool,
        dry_run: bool,
    ):
        self.client = client
        self.state = state
        self.interval = interval
        self.progress_interval = progress_interval
        self.max_commands = max_commands
        self.once = once
        self.dry_run = dry_run
        self.stopping = threading.Event()

    def stop(self) -> None:
        self.stopping.set()

    def run(self) -> int:
        while not self.stopping.is_set():
            try:
                self.poll_once()
            except SlackAuthError as exc:
                LOG.error("AUTH ERROR: %s", exc)
                if self.once:
                    return 3
            except (SlackMCPError, ClaudeRunError) as exc:
                LOG.error("%s", exc)
                if self.once:
                    return 2
            if self.once:
                return 0
            self.stopping.wait(self.interval)
        return 0

    def poll_once(self) -> None:
        LOG.info("Checking Slack self-DM for %s commands", COMMAND_PREFIX)
        channel_id, triggers = self.client.discover(
            str(self.state.data.get("self_dm_channel_id", "")),
            self.max_commands,
        )
        self.state.set_self_dm_channel(channel_id)
        pending = [trigger for trigger in triggers if not self.state.is_handled(trigger)]
        if not pending:
            LOG.info("No unhandled commands found")
            return
        LOG.info("Found %d unhandled command(s)", len(pending))
        for trigger in pending:
            if self.stopping.is_set():
                return
            if self.dry_run:
                LOG.info(
                    "DRY RUN %s in thread %s: %s",
                    trigger.message_ts,
                    trigger.thread_ts,
                    compact_text(trigger.task, 300),
                )
                continue
            self.handle_trigger(trigger)

    def handle_trigger(self, trigger: Trigger) -> None:
        session_id = self.state.session_for(trigger)
        self.state.mark_message(trigger, "processing")
        action = "Resuming session" if session_id else "Starting task"
        acknowledgement = (
            f"{action}: {compact_text(trigger.task, 500)}"
            + (f"\nSession: `{session_id}`" if session_id else "")
        )
        try:
            self.client.post_thread(trigger, acknowledgement)
        except SlackMCPError as exc:
            self.state.mark_message(trigger, "error", str(exc))
            raise

        poster = AsyncPoster(self.client, trigger)
        pending_progress: list[str] = []
        last_progress_at = 0.0

        def remember_session(new_session_id: str) -> None:
            self.state.set_session(trigger, new_session_id)

        def progress(update: str) -> None:
            nonlocal last_progress_at
            if update not in pending_progress:
                pending_progress.append(update)
            now = time.monotonic()
            if now - last_progress_at >= self.progress_interval:
                poster.post("Progress: " + " • ".join(pending_progress[-4:]))
                pending_progress.clear()
                last_progress_at = now

        try:
            result = self.client.run_task(
                trigger,
                session_id,
                on_session=remember_session,
                on_progress=progress,
            )
        except BaseException as exc:
            if pending_progress:
                poster.post("Progress: " + " • ".join(pending_progress[-4:]))
            message = f"Error: Claude Code run was interrupted: {compact_text(exc, 900)}"
            poster.post(message)
            errors = poster.finish()
            self.state.mark_message(trigger, "error", message)
            if errors:
                LOG.error("Could not deliver all Slack updates: %s", "; ".join(errors))
            raise

        if result.session_id:
            self.state.set_session(trigger, result.session_id)
        if pending_progress:
            poster.post("Progress: " + " • ".join(pending_progress[-4:]))
        if result.ok:
            final_message = f"Done: {result.summary}"
            status = "done"
        else:
            final_message = f"Error: Claude Code run failed: {result.summary}"
            status = "error"
        poster.post(final_message)
        post_errors = poster.finish()
        self.state.mark_message(trigger, status, result.summary)
        if post_errors:
            LOG.error("Could not deliver all Slack updates: %s", "; ".join(post_errors))
        if not result.ok:
            raise ClaudeRunError(final_message)


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Poll your Slack self-DM for !run: messages and execute them with "
            "Claude Code."
        )
    )
    parser.add_argument(
        "--interval",
        type=positive_int,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"polling interval in seconds (default: {DEFAULT_INTERVAL_SECONDS})",
    )
    parser.add_argument(
        "--progress-interval",
        type=positive_int,
        default=DEFAULT_PROGRESS_INTERVAL_SECONDS,
        help=(
            "minimum seconds between progress posts "
            f"(default: {DEFAULT_PROGRESS_INTERVAL_SECONDS})"
        ),
    )
    parser.add_argument(
        "--max-commands",
        type=positive_int,
        default=20,
        help="maximum recent command messages to ask Slack for per poll (default: 20)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=default_state_path(),
        help="local JSON state path",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path.cwd(),
        help="directory in which task sessions run (default: current directory)",
    )
    parser.add_argument(
        "--claude-bin",
        default=os.environ.get("CLAUDE_BIN", "claude"),
        help="Claude Code executable (default: claude or CLAUDE_BIN)",
    )
    parser.add_argument(
        "--helper-model",
        default=os.environ.get("SLACK_CLAUDE_HELPER_MODEL", "haiku"),
        help="model for Slack read/write helper calls (default: haiku)",
    )
    parser.add_argument(
        "--task-model",
        default=os.environ.get("SLACK_CLAUDE_TASK_MODEL"),
        help="optional model override for task runs",
    )
    parser.add_argument(
        "--permission-mode",
        choices=[
            "auto",
            "acceptEdits",
            "dontAsk",
            "manual",
            "default",
            "plan",
        ],
        default="auto",
        help="Claude task permission mode (default: auto)",
    )
    parser.add_argument(
        "--max-budget-usd",
        type=float,
        help="optional per-task Claude API spending cap",
    )
    parser.add_argument(
        "--helper-timeout",
        type=positive_int,
        default=DEFAULT_HELPER_TIMEOUT_SECONDS,
        help=(
            "timeout for each Slack helper call in seconds "
            f"(default: {DEFAULT_HELPER_TIMEOUT_SECONDS})"
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="poll once and exit (for cron)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="discover and print unhandled commands without running or marking them",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    return parser


def resolve_claude_binary(value: str) -> str:
    if os.path.sep in value:
        path = Path(value).expanduser().resolve()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise RunnerError(f"Claude executable is not runnable: {path}")
        return str(path)
    found = shutil.which(value)
    if not found:
        raise RunnerError(
            f"Cannot find `{value}`. Install Claude Code or pass --claude-bin."
        )
    return found


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.max_budget_usd is not None and args.max_budget_usd <= 0:
        LOG.error("--max-budget-usd must be greater than zero")
        return 2
    try:
        workdir = args.workdir.expanduser().resolve(strict=True)
        if not workdir.is_dir():
            raise RunnerError(f"Work directory is not a directory: {workdir}")
        os.chdir(workdir)
        state = StateStore(args.state_file)
        state.acquire_lock()
        state.load()
        client = ClaudeClient(
            claude_bin=resolve_claude_binary(args.claude_bin),
            helper_model=args.helper_model or None,
            helper_timeout=args.helper_timeout,
            permission_mode=args.permission_mode,
            task_model=args.task_model,
            max_budget_usd=args.max_budget_usd,
        )
        runner = Runner(
            client=client,
            state=state,
            interval=args.interval,
            progress_interval=args.progress_interval,
            max_commands=args.max_commands,
            once=args.once,
            dry_run=args.dry_run,
        )

        def request_stop(signum: int, _frame: Any) -> None:
            LOG.info("Received signal %s; stopping", signum)
            runner.stop()

        signal.signal(signal.SIGTERM, request_stop)
        return runner.run()
    except KeyboardInterrupt:
        LOG.info("Interrupted")
        return 130
    except (OSError, RunnerError) as exc:
        LOG.error("%s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
