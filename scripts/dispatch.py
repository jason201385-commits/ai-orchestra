#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ai-orchestra unified dispatcher.

Usage:
    <prompt-via-stdin> | python dispatch.py <provider> [--label NAME] [--model M] [--timeout S]

`<provider>` is any name you declared in config/providers.toml (see
providers.example.toml). Two kinds are supported out of the box:

  * CLI tools you're logged into (claude / codex / grok / gemini / a generic CLI)
  * Any OpenAI-compatible HTTP API (OpenAI, DeepSeek, Groq, OpenRouter,
    NVIDIA NIM, local Ollama/LM Studio, ...)

Every call is metered to data/ledger.jsonl (time, tokens, duration, ok/err).
Secrets are read from environment variables and never written to disk or logs.
"""
import argparse
from collections import Counter
from contextlib import contextmanager
import io
import json
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from config import DATA_DIR, LEDGER, load_providers  # noqa: E402

CLAUDE_LOCK = DATA_DIR / "claude-dispatch.lock"
CLAUDE_MAX_ATTEMPTS = 2
DEADLINE_DRAIN_GRACE_SECONDS = 0.2
DEADLINE_DRAIN_MAX_LINES = 10_000
RESULT_SHUTDOWN_GRACE_SECONDS = 0.2


def log_ledger(rec: dict):
    """Append one metering record. Metadata only: provider/duration/ok/error
    kind. Never the prompt body, never a token value."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rec = _redact_ledger(rec)
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _redact_ledger(rec: dict) -> dict:
    """err strings can carry URL/key fragments — filter secrets before writing
    (best-effort; failure must not block metering)."""
    try:
        from quota_common import redact
        return redact(rec)
    except Exception:
        return rec


def classify_err(err, rc=None) -> str:
    """Bucket a failure into a stable label so the usage report's 'top failure
    reasons' is never just 'unrecorded'. Same ruleset as
    quota_common.classify_ledger_err. Returns 'unclassified' rather than guess."""
    try:
        from quota_common import classify_ledger_err
        return classify_ledger_err(err, rc)
    except Exception:
        return "unclassified"


def run_cmd(cmd, timeout, input_text=None, cwd=None):
    t0 = time.monotonic()
    try:
        p = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            # Distinguish None (inherit stdin) from "" (empty, closed stdin);
            # an empty-string input must still create and close the pipe.
            input=(input_text.encode("utf-8") if input_text is not None else None),
            shell=False, cwd=cwd,
        )
        out = p.stdout.decode("utf-8", errors="replace")
        err = p.stderr.decode("utf-8", errors="replace")
        return p.returncode, out, err, time.monotonic() - t0
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout after {timeout}s", time.monotonic() - t0
    except FileNotFoundError as e:
        return -2, "", str(e), time.monotonic() - t0


class ProviderQueueTimeout(TimeoutError):
    pass


@contextmanager
def provider_serial_lock(provider, wait_timeout, lock_path=None):
    """Serialize a provider across dispatcher processes.

    Claude CLI calls sharing one local login can intermittently stall when they
    overlap. The OS releases this advisory lock if a dispatcher crashes, so a
    stale PID cannot leave the provider permanently blocked.
    """
    if wait_timeout <= 0:
        raise ValueError("wait_timeout must be positive")
    path = Path(lock_path) if lock_path else DATA_DIR / f"{provider}-dispatch.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    if path.stat().st_size == 0:
        handle.write(b"\0")
        handle.flush()

    def try_lock():
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, IOError):
            return False

    acquired = try_lock()
    started = time.monotonic()
    last_heartbeat = started
    if not acquired:
        print(
            f"[{provider}] another request is active; queued...",
            file=sys.stderr,
            flush=True,
        )

    try:
        while not acquired:
            elapsed = time.monotonic() - started
            if elapsed >= wait_timeout:
                raise ProviderQueueTimeout(
                    f"{provider} queue wait exceeded {int(wait_timeout)}s"
                )
            time.sleep(min(0.25, max(0.01, wait_timeout - elapsed)))
            acquired = try_lock()
            now = time.monotonic()
            if not acquired and now - last_heartbeat >= 15:
                print(
                    f"[{provider}] queued behind another request... {int(now - started)}s",
                    file=sys.stderr,
                    flush=True,
                )
                last_heartbeat = now

        waited = time.monotonic() - started
        if waited >= 0.05:
            print(
                f"[{provider}] queue acquired after {waited:.1f}s",
                file=sys.stderr,
                flush=True,
            )
        yield waited
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, IOError):
                pass
        handle.close()


def create_windows_kill_job(process):
    """Assign a process to a Windows Job Object that kills descendants on close."""
    if os.name != "nt":
        return None

    import ctypes
    from ctypes import wintypes

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None

    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    configured = kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info)
    )
    assigned = configured and kernel32.AssignProcessToJobObject(
        job, wintypes.HANDLE(int(process._handle))
    )
    if not assigned:
        kernel32.CloseHandle(job)
        return None
    return kernel32, job


def close_windows_job(job_context, terminate=False):
    if not job_context:
        return
    kernel32, job = job_context
    if terminate:
        kernel32.TerminateJobObject(job, 1)
    kernel32.CloseHandle(job)


def terminate_process_tree(process, windows_job=None):
    """Best-effort termination of a provider process and all descendants."""
    if windows_job:
        close_windows_job(windows_job, terminate=True)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass
        return
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def run_streaming_json_cmd(cmd, timeout, provider, input_text=None):
    """Run a JSONL CLI with visible startup/heartbeat messages and keep its final result."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    t0 = time.monotonic()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    except FileNotFoundError as e:
        return -2, "", str(e), time.monotonic() - t0

    windows_job = create_windows_kill_job(process)
    if os.name == "nt" and windows_job is None:
        terminate_process_tree(process)
        return -3, "", "failed to create Windows process job", time.monotonic() - t0

    lines = queue.Queue()

    def read_output():
        try:
            for line in process.stdout:
                lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()

    writer = None
    if input_text is not None:
        def write_input():
            try:
                process.stdin.write(input_text)
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    process.stdin.close()
                except (AttributeError, OSError, ValueError):
                    pass

        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()
    deadline = t0 + timeout
    last_heartbeat = t0
    final_result = ""
    diagnostic_codes = set()
    event_counts = Counter()
    tool_counts = Counter()
    last_event_at = None
    last_complete_assistant_text = ""
    stream_closed = False

    def safe_event_type(value):
        known = {
            "assistant", "error", "rate_limit_event", "result",
            "stream_event", "system", "user",
        }
        return value if isinstance(value, str) and value in known else "other"

    def record_tool_activity(value):
        if isinstance(value, dict):
            if value.get("type") == "tool_use":
                name = value.get("name")
                tool_counts[name if name in {"Read", "Glob", "Grep"} else "other"] += 1
            for nested in value.values():
                record_tool_activity(nested)
        elif isinstance(value, list):
            for nested in value:
                record_tool_activity(nested)

    def activity_summary(now):
        events = ",".join(
            f"{name}:{count}" for name, count in sorted(event_counts.items())
        ) or "none"
        tools = ",".join(
            f"{name}:{count}" for name, count in sorted(tool_counts.items())
        ) or "none"
        age = "none" if last_event_at is None else f"{now - last_event_at:.1f}s"
        assistant_chars = len(last_complete_assistant_text)
        return (
            f"events={events}; tools={tools}; "
            f"assistant_text_chars={assistant_chars}; last_event_age={age}"
        )

    def complete_assistant_text(event):
        """Return only a complete text-only assistant message, never thinking/tools."""
        if event.get("type") != "assistant":
            return ""
        message = event.get("message")
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if not isinstance(content, list):
            return ""
        if any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in content
        ):
            return ""
        texts = [
            block.get("text")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block.get("text")
        ]
        return "\n".join(texts).strip()

    def consume_line(line):
        nonlocal stream_closed, final_result, last_event_at
        nonlocal last_complete_assistant_text
        if line is None:
            stream_closed = True
            return
        if not line:
            return
        stripped = line.strip()
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            diagnostic_codes.add("non_json_output")
            return
        if not isinstance(event, dict):
            diagnostic_codes.add("unexpected_json_root")
            return
        event_type = event.get("type")
        event_counts[safe_event_type(event_type)] += 1
        record_tool_activity(event)
        last_event_at = time.monotonic()
        if last_complete_assistant_text and event_type != "result":
            # Fail closed: a fallback is valid only while the latest semantic
            # event remains that complete text-only assistant. Stream deltas,
            # errors, user/tool activity, and unknown nonterminal events all
            # invalidate it.
            last_complete_assistant_text = ""
        if event_type == "assistant":
            # The fallback must be the latest complete assistant event. A
            # later tool-use/non-text assistant invalidates older text.
            last_complete_assistant_text = complete_assistant_text(event)
        if event_type == "result":
            final_result = stripped
        elif event_type == "system" and event.get("subtype") == "init":
            model = event.get("model") or "default"
            print(f"[{provider}] started ({model})", file=sys.stderr, flush=True)
        elif event_type == "error" or event.get("is_error"):
            diagnostic_codes.add("provider_error_event")

    def cleanup_process():
        nonlocal windows_job
        if process.poll() is None:
            terminate_process_tree(process, windows_job=windows_job)
            windows_job = None
        elif windows_job:
            close_windows_job(windows_job)
            windows_job = None

        # Do not close TextIOWrapper while its reader thread owns the blocking
        # read lock. Process-tree termination closes the writer end, so EOF
        # should let the reader finish without multi-second lock contention.
        reader.join(timeout=0.25)
        if reader.is_alive():
            try:
                os.close(process.stdout.fileno())
            except (AttributeError, OSError, ValueError):
                pass
            reader.join(timeout=0.25)
        if not reader.is_alive():
            try:
                process.stdout.close()
            except (AttributeError, OSError, ValueError):
                pass
        if writer:
            writer.join(timeout=0.25)
            if writer.is_alive():
                try:
                    os.close(process.stdin.fileno())
                except (AttributeError, OSError, ValueError):
                    pass
                writer.join(timeout=0.25)

    def finish_complete_result():
        # A result event is authoritative. Give the CLI only a short bounded
        # shutdown grace, then clean up descendants instead of waiting for the
        # full task deadline.
        shutdown_deadline = time.monotonic() + RESULT_SHUTDOWN_GRACE_SECONDS
        while process.poll() is None and time.monotonic() < shutdown_deadline:
            remaining = shutdown_deadline - time.monotonic()
            try:
                extra_line = lines.get(
                    timeout=min(0.01, max(0.001, remaining))
                )
            except queue.Empty:
                continue
            consume_line(extra_line)
        cleanup_process()
        err = ",".join(sorted(diagnostic_codes))
        return 0, final_result, err, time.monotonic() - t0

    def bounded_deadline_drain():
        drain_deadline = time.monotonic() + DEADLINE_DRAIN_GRACE_SECONDS
        drained = 0
        while drained < DEADLINE_DRAIN_MAX_LINES and time.monotonic() < drain_deadline:
            remaining = drain_deadline - time.monotonic()
            try:
                line = lines.get(timeout=min(0.01, max(0.001, remaining)))
            except queue.Empty:
                if final_result:
                    break
                continue
            consume_line(line)
            drained += 1
            if final_result:
                break

    while not stream_closed or process.poll() is None:
        now = time.monotonic()
        if now >= deadline:
            # A result may already be queued while the process is still doing
            # shutdown work. Drain a bounded amount before declaring timeout.
            bounded_deadline_drain()
            if final_result:
                return finish_complete_result()

            cleanup_process()
            partial = ""
            partial_marker = ""
            if last_complete_assistant_text:
                partial = json.dumps({
                    "type": "incomplete_result",
                    "result": last_complete_assistant_text,
                }, ensure_ascii=False)
                partial_marker = "; partial_assistant_text=yes"
            return (
                -1, partial,
                f"timeout after {timeout}s; {activity_summary(time.monotonic())}"
                f"{partial_marker}",
                time.monotonic() - t0,
            )

        try:
            line = lines.get(timeout=min(0.5, max(0.05, deadline - now)))
        except queue.Empty:
            line = ""

        consume_line(line)

        if final_result:
            return finish_complete_result()

        now = time.monotonic()
        if process.poll() is None and now - last_heartbeat >= 15:
            print(
                f"[{provider}] working... {int(now - t0)}s "
                f"({activity_summary(now)})",
                file=sys.stderr,
                flush=True,
            )
            last_heartbeat = now

    rc = process.wait()
    reader.join(timeout=1)
    if writer:
        writer.join(timeout=1)
    try:
        process.stdout.close()
    except (AttributeError, OSError, ValueError):
        pass
    close_windows_job(windows_job)
    err = ",".join(sorted(diagnostic_codes))
    if not final_result and not err:
        err = f"{provider} exited without a result event"
    return rc, final_result, err, time.monotonic() - t0


def find_exe(name):
    """On Windows, npm-installed CLIs are .ps1/.cmd; subprocess needs the .cmd
    or a full path."""
    from shutil import which
    for cand in (name, name + ".cmd", name + ".exe"):
        p = which(cand)
        if p:
            return p
    return name


def deep_find(obj, key_patterns):
    """Find keys matching any pattern in nested dict/list; return {key: value}."""
    found = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            for pat in key_patterns:
                if re.search(pat, k, re.I) and isinstance(v, (int, float, str, dict)):
                    found[k] = v
            found.update(deep_find(v, key_patterns))
    elif isinstance(obj, list):
        for item in obj:
            found.update(deep_find(item, key_patterns))
    return found


# ── providers ──────────────────────────────────────────────
# Every adapter has the signature (spec, prompt, model, timeout, **opts) and
# returns (rc, text, err, duration, usage, rate). rc == 0 and non-empty text
# means success.

def parse_json_blobs(out):
    """Accept either one pretty-printed JSON doc or line-delimited JSONL."""
    blobs = []
    try:
        blobs.append(json.loads(out))
        return blobs
    except json.JSONDecodeError:
        pass
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            blobs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return blobs


def call_grok(spec, prompt, model, timeout):
    exe = find_exe(spec.get("command") or "grok")
    cmd = [exe, "-p", prompt, "--output-format", "json"]
    if model:
        cmd += ["--model", model]
    rc, out, err, dur = run_cmd(cmd, timeout)
    text, usage, rate = "", {}, {}
    for j in parse_json_blobs(out):
        u = deep_find(j, [r"token", r"usage"])
        usage.update({k: v for k, v in u.items() if isinstance(v, (int, float))})
        for key in ("text", "content", "result", "response", "message"):
            if isinstance(j.get(key), str) and j[key]:
                text = j[key]
                break
    if not text:
        text = out.strip()
    if rc != 0 and not text:
        rc, out, err, dur2 = run_cmd([exe, "-p", prompt], timeout)
        dur += dur2
        text = out.strip()
    return rc, text, err, dur, usage, rate


def call_codex(spec, prompt, model, timeout):
    exe = find_exe(spec.get("command") or "codex")
    cmd = [exe, "exec", "--skip-git-repo-check", "--json"]
    if model:
        cmd += ["-m", model]
    cmd.append(prompt)
    rc, out, err, dur = run_cmd(cmd, timeout, input_text="")
    text, usage, rate = "", {}, {}
    for j in parse_json_blobs(out):
        # token usage event: {"type":"turn.completed","usage":{...}}
        u = deep_find(j, [r"tokens?$", r"input_tokens", r"output_tokens", r"total_tokens", r"cached"])
        usage.update({k: v for k, v in u.items() if isinstance(v, (int, float))})
        r = deep_find(j, [r"rate_limit", r"used_percent", r"resets_", r"window"])
        rate.update({k: v for k, v in r.items() if isinstance(v, (int, float, str))})
        # final message: {"type":"item.completed","item":{"type":"agent_message","text":"..."}}
        item = j.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            text = item["text"]
    if not text:
        plain = [ln for ln in out.splitlines() if ln.strip() and not ln.strip().startswith("{")]
        text = "\n".join(plain).strip()
    return rc, text, err, dur, usage, rate


def call_gemini(spec, prompt, model, timeout):
    exe = find_exe(spec.get("command") or "gemini")
    cmd = [exe, "--skip-trust", "-p", prompt]
    if model:
        cmd += ["-m", model]
    rc, out, err, dur = run_cmd(cmd, timeout)
    blob = out + err
    if "IneligibleTierError" in blob or "Error authenticating" in blob:
        return 55, "", (
            "Gemini CLI auth failed. Fix: set GEMINI_API_KEY, or re-run the "
            "interactive `gemini` login."
        ), dur, {}, {}
    return rc, out.strip(), err, dur, {}, {}


def call_generic_cli(spec, prompt, model, timeout):
    """Any command-line tool. `args` may contain {prompt} / {model} placeholders;
    if there's no {prompt} placeholder, the prompt is either piped to stdin
    (stdin=true) or appended as the final argument."""
    exe = find_exe(spec.get("command") or spec["name"])
    resolved_model = model or spec.get("model") or ""
    final_args, substituted = [], False
    for a in (spec.get("args") or []):
        a = str(a)
        if "{prompt}" in a:
            final_args.append(a.replace("{prompt}", prompt))
            substituted = True
        elif "{model}" in a:
            final_args.append(a.replace("{model}", resolved_model))
        else:
            final_args.append(a)
    cmd = [exe] + final_args
    use_stdin = bool(spec.get("stdin"))
    input_text = None
    if not substituted:
        if use_stdin:
            input_text = prompt
        else:
            cmd.append(prompt)
    rc, out, err, dur = run_cmd(cmd, timeout, input_text=input_text)
    return rc, out.strip(), err, dur, {}, {}


def call_openai(spec, prompt, model, timeout):
    """One adapter for every OpenAI-compatible /chat/completions endpoint."""
    import urllib.request
    import urllib.error
    from urllib.parse import urlparse
    base = spec.get("base_url")
    if not base:
        return 1, "", f"{spec['name']}: base_url not set", 0, {}, {}
    key_env = spec.get("api_key_env") or ""
    key = os.environ.get(key_env, "") if key_env else ""
    if key_env and not key:
        return 1, "", f"{key_env} not set in environment", 0, {}, {}
    # Refuse to send a key over a plaintext/non-HTTPS URL (localhost excepted).
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    is_local = host in ("localhost", "127.0.0.1", "::1") or host.endswith(".localhost")
    if key and parsed.scheme != "https" and not is_local:
        return 1, "", (
            f"{spec['name']}: refusing to send an API key over non-HTTPS base_url "
            f"({parsed.scheme or 'no-scheme'}://{host}); use https:// or a localhost endpoint"
        ), 0, {}, {}
    model = model or spec.get("model")
    if not model:
        return 1, "", f"{spec['name']}: no model configured", 0, {}, {}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    for hk, hv in (spec.get("extra_headers") or {}).items():
        headers[str(hk)] = str(hv)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    # The token-cap parameter is optional and its name varies — OpenAI's newer
    # models want "max_completion_tokens". Omit entirely when max_tokens <= 0.
    max_tokens = int(spec.get("max_tokens", 4096))
    if max_tokens > 0:
        payload[spec.get("max_tokens_param") or "max_tokens"] = max_tokens
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + "/chat/completions", data=body, headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            j = json.loads(resp.read().decode("utf-8"))
            rate = {k: v for k, v in resp.headers.items()
                    if "ratelimit" in k.lower() or "x-request" in k.lower()}
            usage = j.get("usage", {}) or {}
            text = j["choices"][0]["message"]["content"]
            return 0, text, "", time.time() - t0, usage, rate
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return 1, "", f"HTTP {e.code} {detail}".strip(), time.time() - t0, {}, {}
    except (KeyError, IndexError, TypeError) as e:
        return 1, "", f"unexpected response shape: {e}", time.time() - t0, {}, {}
    except Exception as e:
        return 1, "", str(e), time.time() - t0, {}, {}
    finally:
        key = None
        del key


def call_claude(
    spec, prompt, model, timeout, retry_no_result=False, profile="review",
    effort="", max_budget_usd=None,
):
    exe = find_exe(spec.get("command") or "claude")
    if profile == "text":
        reviewer_prompt = (
            "Answer the request directly without using tools. "
            "Do not reveal secret values.\n\n" + prompt
        )
        tools = ""
    else:
        reviewer_prompt = (
            "Act as an independent read-only reviewer. "
            "If AGENTS.md exists in the working directory, read and obey it before analysis. "
            "Do not modify files and do not reveal secret values.\n\n"
            + prompt
        )
        tools = "Read,Glob,Grep"
    cmd = [
        exe, "-p",
        "--safe-mode", "--permission-mode", "plan",
        "--tools", tools,
        "--no-session-persistence", "--no-chrome",
        "--output-format", "stream-json", "--verbose",
        "--include-partial-messages",
    ]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    if max_budget_usd is not None:
        cmd += ["--max-budget-usd", str(max_budget_usd)]
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    overall_t0 = time.monotonic()
    attempts = 0
    queued_for = 0.0
    try:
        with provider_serial_lock("claude", timeout, CLAUDE_LOCK) as queued_for:
            first_attempt_timeout = timeout - queued_for
            if first_attempt_timeout <= 0:
                raise ProviderQueueTimeout(
                    f"claude queue wait exceeded {timeout}s"
                )
            max_attempts = CLAUDE_MAX_ATTEMPTS if retry_no_result else 1
            for attempt in range(1, max_attempts + 1):
                attempts += 1
                attempt_timeout = first_attempt_timeout if attempt == 1 else timeout
                rc, out, err, _attempt_dur = run_streaming_json_cmd(
                    cmd, attempt_timeout, "claude", input_text=reviewer_prompt
                )
                no_result_timeout = (
                    rc == -1 and not out and err.startswith("timeout after ")
                )
                if not no_result_timeout or attempt == max_attempts:
                    break
                print(
                    "[claude] no result before timeout; opt-in retrying once...",
                    file=sys.stderr,
                    flush=True,
                )
    except ProviderQueueTimeout as e:
        dur = time.monotonic() - overall_t0
        usage = {"dispatch": {
            "attempts": 0,
            "queued_seconds": round(max(queued_for, dur), 3),
            "retried": False,
            "usage_complete": False,
        }}
        return -4, "", str(e), dur, usage, {}

    dur = time.monotonic() - overall_t0
    text, usage = out.strip(), {}
    result_event = None
    try:
        j = json.loads(out)
        result_event = j
        text = j.get("result", text)
        u = deep_find(j, [r"tokens", r"usage", r"cost"])
        usage = {k: v for k, v in u.items() if isinstance(v, (int, float))}
    except (json.JSONDecodeError, AttributeError):
        pass
    usage_complete = (
        attempts == 1
        and isinstance(result_event, dict)
        and result_event.get("type") == "result"
        and not result_event.get("is_error")
        and bool(usage)
    )
    usage["dispatch"] = {
        "attempts": attempts,
        "queued_seconds": round(queued_for, 3),
        "retried": attempts > 1,
        "usage_complete": usage_complete,
    }
    if isinstance(result_event, dict) and result_event.get("is_error"):
        raw_subtype = result_event.get("subtype")
        allowed_subtypes = {
            "error", "api_error", "authentication_error",
            "rate_limit_error", "overloaded_error",
        }
        subtype = raw_subtype if raw_subtype in allowed_subtypes else "api_error"
        status = result_event.get("api_error_status")
        suffix = f", status={status}" if isinstance(status, int) else ""
        return 1, "", f"claude CLI error ({subtype}{suffix})", dur, usage, {}
    return rc, text, err, dur, usage, {}


# kind -> adapter for CLI providers
CLI_ADAPTERS = {
    "claude": call_claude,
    "codex": call_codex,
    "grok": call_grok,
    "gemini": call_gemini,
    "generic": call_generic_cli,
}


def doctor(providers):
    """Offline readiness check for each enabled provider: is the CLI on PATH,
    is the API key set, is the base_url safe? Makes NO network calls."""
    from shutil import which
    from urllib.parse import urlparse
    rows = []
    for name in sorted(providers):
        s = providers[name]
        if not s.get("enabled", True):
            rows.append((name, s["type"], "skip", "disabled in providers.toml"))
            continue
        if s["type"] == "cli":
            cmd = s.get("command") or name
            found = which(cmd) or which(cmd + ".cmd") or which(cmd + ".exe")
            if found:
                rows.append((name, f"cli:{s.get('kind')}", "OK",
                             f"{cmd} on PATH — ensure you're logged in"))
            else:
                rows.append((name, f"cli:{s.get('kind')}", "MISSING",
                             f"'{cmd}' not on PATH — install/login the CLI"))
        elif s["type"] == "openai":
            base = s.get("base_url") or ""
            key_env = s.get("api_key_env") or ""
            host = (urlparse(base).hostname or "").lower()
            is_local = (host in ("localhost", "127.0.0.1", "::1")
                        or host.endswith(".localhost"))
            has_key = bool(os.environ.get(key_env)) if key_env else True
            if not base:
                rows.append((name, "openai", "BAD", "base_url not set"))
            elif key_env and not has_key:
                rows.append((name, "openai", "NO KEY", f"set env var {key_env}"))
            elif key_env and urlparse(base).scheme != "https" and not is_local:
                rows.append((name, "openai", "INSECURE",
                             f"key over non-HTTPS {base} — use https or localhost"))
            else:
                rows.append((name, "openai", "OK",
                             base + ("  (keyless local)" if not key_env else "")))
        else:
            rows.append((name, s["type"], "BAD", "unsupported type"))
    w = max((len(r[0]) for r in rows), default=8) + 2
    print(f"{'provider':<{w}}{'type':<13}{'status':<9}note")
    print("-" * (w + 45))
    ready = 0
    for name, typ, status, note in rows:
        ready += status == "OK"
        print(f"{name:<{w}}{typ:<13}{status:<9}{note}")
    enabled = [r for r in rows if r[2] != "skip"]
    print(f"\n{ready}/{len(enabled)} enabled provider(s) ready "
          f"({len(rows) - len(enabled)} disabled).")


def normalize_stdin_prompt(prompt):
    """Remove only transport BOMs; preserve all user whitespace/content."""
    return prompt.lstrip("﻿")


def looks_like_question_mark_mojibake(prompt):
    """Detect PowerShell's common CJK-to-question-mark pipe corruption."""
    meaningful = [char for char in prompt if not char.isspace()]
    if len(meaningful) < 2:
        return False
    question_count = sum(char in {"?", "？"} for char in meaningful)
    if question_count == len(meaningful):
        return True
    runs = re.findall(r"[?？]{2,}", prompt)
    if question_count < 6 or not runs:
        return False
    ratio = question_count / len(meaningful)
    longest_run = max(len(run) for run in runs)
    return ratio >= 0.12 or longest_run >= 4


def should_reject_suspicious_prompt(prompt, utf8_transport_verified=False):
    return (
        not utf8_transport_verified
        and looks_like_question_mark_mojibake(prompt)
    )


def main():
    def positive_timeout(value):
        try:
            parsed = int(value)
        except ValueError as e:
            raise argparse.ArgumentTypeError("timeout must be an integer") from e
        if parsed <= 0:
            raise argparse.ArgumentTypeError("timeout must be greater than zero")
        return parsed

    def positive_float(value):
        try:
            parsed = float(value)
        except ValueError as e:
            raise argparse.ArgumentTypeError("value must be a number") from e
        if not math.isfinite(parsed) or parsed <= 0:
            raise argparse.ArgumentTypeError(
                "value must be finite and greater than zero"
            )
        return parsed

    providers = load_providers()

    ap = argparse.ArgumentParser(
        description="Dispatch a prompt (via stdin) to one configured provider.",
    )
    ap.add_argument("provider", nargs="?", help="a provider name from config/providers.toml")
    ap.add_argument("--label", default="")
    ap.add_argument("--model", default="")
    ap.add_argument(
        "--timeout", type=positive_timeout, default=None,
        help="total seconds for queue wait plus the first provider attempt "
             "(default: the provider's default_timeout, or 300)",
    )
    ap.add_argument(
        "--list", action="store_true",
        help="list configured providers and exit",
    )
    ap.add_argument(
        "--doctor", action="store_true",
        help="check each enabled provider's readiness (offline) and exit",
    )
    ap.add_argument(
        "--retry-no-result", action="store_true",
        help=(
            "Claude only: retry once after a no-result timeout; opt-in because "
            "it can add one full timeout and another billed model call"
        ),
    )
    ap.add_argument(
        "--claude-profile", choices=("review", "text"), default="review",
        help="Claude only: read-only project review or no-tools text response",
    )
    ap.add_argument(
        "--effort", choices=("low", "medium", "high", "xhigh", "max"),
        default="", help="Claude only: explicit reasoning effort",
    )
    ap.add_argument(
        "--max-budget-usd", type=positive_float, default=None,
        help="Claude only: set the CLI's print-mode spend budget",
    )
    args = ap.parse_args()

    if args.list:
        if not providers:
            print("(no providers configured — copy providers.example.toml "
                  "to config/providers.toml)")
            return
        for name in sorted(providers):
            s = providers[name]
            detail = s["kind"] if s["type"] == "cli" else f"openai:{s.get('base_url')}"
            flag = "" if s.get("enabled", True) else " (disabled)"
            role = f"  — {s['role']}" if s.get("role") else ""
            print(f"{name:<16}{s['type']:<8}{detail}{flag}{role}")
        return

    if args.doctor:
        if not providers:
            print("(no providers configured — copy providers.example.toml "
                  "to config/providers.toml)")
            return
        doctor(providers)
        return

    if not args.provider:
        ap.error("a provider name is required (see --list)")

    if not providers:
        ap.error(
            "no providers configured. Copy providers.example.toml to "
            "config/providers.toml and enable the ones you have."
        )
    spec = providers.get(args.provider)
    if spec is None:
        ap.error(
            f"unknown provider '{args.provider}'. Configured: "
            f"{', '.join(sorted(providers)) or '(none)'}"
        )
    if not spec.get("enabled", True):
        ap.error(f"provider '{args.provider}' is disabled in providers.toml")

    is_claude_adapter = spec["type"] == "cli" and spec.get("kind") == "claude"
    claude_specific_requested = (
        args.retry_no_result
        or args.claude_profile != "review"
        or bool(args.effort)
        or args.max_budget_usd is not None
    )
    if claude_specific_requested and not is_claude_adapter:
        ap.error("Claude-specific options are only valid for a kind=claude provider")

    timeout = args.timeout or int(spec.get("default_timeout", 300))

    utf8_transport_verified = (
        os.environ.pop("AI_ORCHESTRA_UTF8_STDIN_VERIFIED", "") == "1"
    )
    prompt = normalize_stdin_prompt(sys.stdin.read())
    if not prompt.strip():
        ap.error("prompt is required via stdin")
    if should_reject_suspicious_prompt(prompt, utf8_transport_verified):
        ap.error(
            "prompt appears to be question-mark mojibake; on Windows "
            "PowerShell use scripts/dispatch.ps1 so stdin is emitted as UTF-8"
        )

    model = args.model or spec.get("model") or ""

    if is_claude_adapter:
        rc, text, err, dur, usage, rate = call_claude(
            spec, prompt, model, timeout,
            retry_no_result=args.retry_no_result,
            profile=args.claude_profile,
            effort=args.effort,
            max_budget_usd=args.max_budget_usd,
        )
    elif spec["type"] == "openai":
        rc, text, err, dur, usage, rate = call_openai(spec, prompt, model, timeout)
    elif spec["type"] == "cli":
        adapter = CLI_ADAPTERS.get(spec.get("kind"), call_generic_cli)
        rc, text, err, dur, usage, rate = adapter(spec, prompt, model, timeout)
    else:
        ap.error(f"provider '{args.provider}' has unsupported type '{spec['type']}'")

    rec = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "provider": args.provider,
        "type": spec["type"],
        "label": args.label,
        "model": model or None,
        "prompt_chars": len(prompt),
        "output_chars": len(text),
        "usage": usage or None,
        "rate_snapshot": rate or None,
        "duration_s": round(dur, 1),
        "ok": rc == 0 and bool(text),
        "err": (err or "")[:500] if rc != 0 else None,
        "err_kind": classify_err(err, rc) if rc != 0 else None,
        "rc": rc,
    }
    if is_claude_adapter:
        rec["dispatch_options"] = {
            "profile": args.claude_profile,
            "effort": args.effort or None,
            "max_budget_usd": args.max_budget_usd,
            "retry_no_result": args.retry_no_result,
        }
    log_ledger(rec)

    if rec["ok"]:
        print(text)
    else:
        if (
            is_claude_adapter
            and text
            and "partial_assistant_text=yes" in (err or "")
        ):
            print(
                f"[dispatch INCOMPLETE rc={rc}] {err[:800]}",
                file=sys.stderr,
            )
            print(text)
        else:
            print(f"[dispatch FAILED rc={rc}] {err[:800]}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
