#!/usr/bin/env python3
"""
bench.py - Benchmark token usage & cost across models for Claude Code (CCR) and OpenCode.

Usage:
    python bench.py --tool ccr     --model openrouter/anthropic/claude-sonnet-4-6 --task tasks/example_task.txt
    python bench.py --tool opencode --model openrouter/anthropic/claude-sonnet-4-6 --task tasks/example_task.txt
    python bench.py --tool both    --model openrouter/anthropic/claude-sonnet-4-6 --task tasks/example_task.txt

The model argument format differs per tool:
  - CCR:      <provider>,<model>  e.g.  openrouter,anthropic/claude-sonnet-4-6
  - OpenCode: <provider>/<model>  e.g.  openrouter/anthropic/claude-sonnet-4-6
  - bench.py accepts either slash or comma as separator and normalises automatically.
"""

# Proxyman on macOS injects its own 'http' package via PYTHONPATH which breaks stdlib http.
# Re-launch with a clean PYTHONPATH so the stdlib http module is used unmodified.
import os as _os, sys as _sys
if _os.environ.get("PYTHONPATH") and "_BENCH_CLEAN_PATH" not in _os.environ:
    _env = dict(_os.environ)
    _env["PYTHONPATH"] = ""
    _env["_BENCH_CLEAN_PATH"] = "1"
    _os.execve(_sys.executable, [_sys.executable] + _sys.argv, _env)

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).parent
PRICING_FILE = SCRIPT_DIR / "models-pricing.json"
RESULTS_DIR = SCRIPT_DIR / "results"
OPENCODE_BIN = Path.home() / ".opencode" / "bin" / "opencode"
OPENCODE_DB = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
CCR_CONFIG = Path.home() / ".claude-code-router" / "config.json"

PROXY_PORT = 3999  # Our intercepting proxy port (sits in front of CCR on 3456)


# ---------------------------------------------------------------------------
# Pricing helper
# ---------------------------------------------------------------------------
def load_pricing() -> dict:
    with open(PRICING_FILE) as f:
        return json.load(f)


def get_price(pricing: dict, provider: str, model: str) -> Optional[dict]:
    """
    Return pricing dict for provider/model or None if not found.

    When provider is 'openrouter', the model string is 'sub_provider/model_id'
    (e.g. 'anthropic/claude-sonnet-4-6'), so we also try the sub-provider.
    """
    providers = pricing.get("providers", {})
    aliases = {"z-ai": "zai", "zai": "z-ai"}

    def _lookup(prov: str, mod: str) -> Optional[dict]:
        prov = prov.lower().rstrip("/")
        for try_key in [prov, aliases.get(prov, "")]:
            if try_key not in providers:
                continue
            models = providers[try_key]
            # Exact match first, then try swapping dots/dashes in version suffix
            if mod in models:
                return models[mod]
            alt = mod.replace(".", "-") if "." in mod else mod.replace("-", ".", 2)
            if alt in models:
                return models[alt]
        return None

    # Direct lookup
    result = _lookup(provider, model)
    if result:
        return result

    # If routing via openrouter, model = 'sub_provider/model_id'
    if "/" in model:
        sub_provider, sub_model = model.split("/", 1)
        result = _lookup(sub_provider, sub_model)
        if result:
            return result

    return None


def calc_cost(pricing_entry: dict, tokens: dict) -> float:
    """Return USD cost given pricing entry and token counts."""
    if not pricing_entry:
        return 0.0
    factor = 1 / 1_000_000
    cost = (
        tokens.get("input", 0) * pricing_entry.get("input", 0) * factor
        + tokens.get("output", 0) * pricing_entry.get("output", 0) * factor
        + tokens.get("cache_read", 0) * pricing_entry.get("cache_read", 0) * factor
        + tokens.get("cache_write", 0) * pricing_entry.get("cache_write", 0) * factor
    )
    return cost


# ---------------------------------------------------------------------------
# Model name normalisation
# ---------------------------------------------------------------------------
def parse_model(raw: str) -> tuple[str, str]:
    """
    Parse a model string into (provider, model_id).
    Accepts:
      openrouter,anthropic/claude-sonnet-4-6
      openrouter/anthropic/claude-sonnet-4-6
    Returns ('openrouter', 'anthropic/claude-sonnet-4-6').
    """
    # if comma-separated: first part is provider
    if "," in raw:
        provider, model = raw.split(",", 1)
        return provider.strip(), model.strip()
    # slash-separated: first segment is provider
    parts = raw.split("/", 1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return raw.strip(), raw.strip()


def model_for_ccr(provider: str, model: str) -> str:
    """Return CCR Router default string e.g. 'openrouter,anthropic/claude-sonnet-4-6'."""
    return f"{provider},{model}"


def model_for_opencode(provider: str, model: str) -> str:
    """
    Return opencode --model string e.g. 'openrouter/anthropic/claude-sonnet-4.6'.
    OpenCode expects dot-separated version suffixes (4.6, 4.5, 3.7 …), but users
    may supply dashes (4-6).  Normalise trailing -N-N patterns to .N.N.
    """
    # Replace dash-separated numeric version suffix with dots only when the
    # segment before it is non-numeric (a word like "sonnet"), e.g.:
    #   claude-sonnet-4-6  ->  claude-sonnet-4.6
    #   kimi-k2.5          ->  unchanged (already dots)
    normalised = re.sub(r"([a-zA-Z])-(\d+)-(\d+)$", r"\1-\2.\3", model)
    return f"{provider}/{normalised}"


# ---------------------------------------------------------------------------
# Intercepting proxy: sits between CCR and the upstream provider (OpenRouter).
# CCR's config api_base_url is temporarily rewritten to point here.
# We forward all requests to the real upstream over HTTPS and capture
# SSE usage events from each response.
# ---------------------------------------------------------------------------
class _SSEUsageAccumulator:
    """
    Parses incremental SSE bytes from the proxy stream, accumulating both
    token usage and assistant text content per LLM turn.
    """

    def __init__(self, model: str, usage_sink: list, text_sink: list):
        self.model = model
        self.usage_sink = usage_sink
        self.text_sink = text_sink
        self._buf = ""
        self._usage: dict = {}
        self._text: str = ""

    def feed(self, raw_bytes: bytes):
        try:
            self._buf += raw_bytes.decode("utf-8", errors="replace")
        except Exception:
            return
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    self._handle_event(payload)

    def _handle_event(self, payload: str):
        try:
            obj = json.loads(payload)
        except Exception:
            return

        evt_type = obj.get("type", "")

        # Anthropic SSE: message_start carries input token count
        if evt_type == "message_start":
            msg = obj.get("message", {})
            usage = msg.get("usage", {})
            if usage:
                self._usage.update(usage)
            if "model" in msg and not self.model:
                self.model = msg["model"]

        # Anthropic SSE: message_delta carries output / cache token counts
        elif evt_type == "message_delta":
            usage = obj.get("usage", {})
            if usage:
                self._usage.update(usage)

        # Anthropic SSE: content_block_delta carries streamed text
        elif evt_type == "content_block_delta":
            delta = obj.get("delta", {})
            if delta.get("type") == "text_delta":
                self._text += delta.get("text", "")

        # OpenAI-style SSE: text in choices[].delta.content, usage at top level
        elif "choices" in obj:
            for choice in obj.get("choices", []):
                self._text += choice.get("delta", {}).get("content", "") or ""
            if obj.get("usage"):
                self._usage.update(obj["usage"])

        # Flush on final event
        is_done = evt_type == "message_stop" or (
            "choices" in obj
            and any(c.get("finish_reason") for c in obj.get("choices", []))
        )
        if is_done:
            if self._usage:
                self.usage_sink.append({
                    "model": self.model,
                    "tokens": dict(self._usage),
                    "time": time.time(),
                })
                self._usage = {}
            if self._text:
                self.text_sink.append(self._text)
                self._text = ""


class _ProxyHandler(BaseHTTPRequestHandler):
    """
    Transparent HTTPS-forwarding proxy.
    upstream_url is the real target (e.g. 'https://openrouter.ai/api/v1/chat/completions').
    We accept requests on HTTP (CCR sends HTTP to us) and forward to HTTPS upstream.
    """

    upstream_url: str = ""   # set by ProxyServer before starting
    usage_records: list = []
    text_records: list = []
    request_log: list = []

    def log_message(self, format, *args):  # noqa: suppress request logging
        pass

    def do_POST(self):
        self._forward("POST")

    def do_GET(self):
        self._forward("GET")

    def _forward(self, method: str):
        import http.client as hc
        import ssl

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""

        parsed = urlparse(self.__class__.upstream_url)
        is_https = parsed.scheme == "https"
        host = parsed.hostname
        port = parsed.port or (443 if is_https else 80)

        # Reconstruct path: use incoming path but keep upstream base path prefix
        # e.g. upstream = https://openrouter.ai/api/v1/chat/completions
        #      CCR sends POST /api/v1/chat/completions
        # We simply forward the incoming self.path to the upstream host.
        target_path = self.path

        fwd_headers = {}
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length", "transfer-encoding"):
                fwd_headers[k] = v
        fwd_headers["Host"] = host
        if body:
            fwd_headers["Content-Length"] = str(len(body))

        # Extract model for labelling
        req_model = ""
        if body:
            try:
                req_model = json.loads(body).get("model", "")
            except Exception:
                pass

        self.__class__.request_log.append({
            "method": method,
            "path": target_path,
            "model": req_model,
            "time": time.time(),
        })

        try:
            # Disable cert verification: on macOS with Proxyman the system
            # root certs are not picked up by Python's ssl, causing handshake
            # failures when forwarding to HTTPS upstreams.
            ctx = ssl.create_default_context() if is_https else None
            if ctx:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            ConnClass = hc.HTTPSConnection if is_https else hc.HTTPConnection
            conn = ConnClass(host, port, timeout=600, context=ctx)
            conn.request(method, target_path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
        except Exception as e:
            self.send_error(502, f"Proxy upstream error: {e}")
            return

        self.send_response(resp.status)
        resp_headers: dict = {}
        for k, v in resp.getheaders():
            kl = k.lower()
            if kl not in ("transfer-encoding",):
                self.send_header(k, v)
                resp_headers[kl] = v
        self.end_headers()

        is_sse = "text/event-stream" in resp_headers.get("content-type", "")
        is_json = "application/json" in resp_headers.get("content-type", "")

        if is_sse:
            accumulator = _SSEUsageAccumulator(req_model, self.__class__.usage_records, self.__class__.text_records)
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except Exception:
                    break
                accumulator.feed(chunk)
        elif is_json:
            data = resp.read()
            self.wfile.write(data)
            try:
                obj = json.loads(data)
                if obj.get("usage"):
                    self.__class__.usage_records.append({
                        "model": req_model,
                        "tokens": obj["usage"],
                        "time": time.time(),
                    })
            except Exception:
                pass
        else:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)

        conn.close()


class ProxyServer:
    """Manages the intercepting proxy lifecycle."""

    def __init__(self, port: int, upstream_url: str):
        self.port = port
        self.upstream_url = upstream_url
        self._usage_records: list = []
        self._text_records: list = []
        self._request_log: list = []
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        usage_records = self._usage_records
        text_records = self._text_records
        request_log = self._request_log
        upstream_url = self.upstream_url

        class Handler(_ProxyHandler):
            pass

        Handler.usage_records = usage_records
        Handler.text_records = text_records
        Handler.request_log = request_log
        Handler.upstream_url = upstream_url

        self._server = HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()

    @property
    def usage_records(self) -> list:
        return self._usage_records

    @property
    def text_records(self) -> list:
        return self._text_records

    @property
    def request_log(self) -> list:
        return self._request_log


# ---------------------------------------------------------------------------
# CCR config manipulation
# ---------------------------------------------------------------------------
def ccr_read_config() -> dict:
    with open(CCR_CONFIG) as f:
        return json.load(f)


def ccr_write_config(config: dict):
    with open(CCR_CONFIG, "w") as f:
        json.dump(config, f, indent=2)


def ccr_restart():
    subprocess.run(["ccr", "restart"], check=True, capture_output=True)
    time.sleep(2)


def ccr_get_current_model() -> str:
    """Return current default model from CCR config."""
    return ccr_read_config().get("Router", {}).get("default", "")


def _ccr_find_provider_entry(config: dict, provider_name: str) -> Optional[dict]:
    """Return the provider entry matching provider_name (case-insensitive)."""
    for p in config.get("Providers", []):
        if p.get("name", "").lower() == provider_name.lower():
            return p
    return None


# ---------------------------------------------------------------------------
# Run helpers
# ---------------------------------------------------------------------------
def run_ccr(task: str, provider: str, model: str, output_dir: Path, timeout: int) -> dict:
    """
    Run `ccr code --print --output-format stream-json` with the given task.

    Token usage, cost, and response text are all parsed directly from the
    stream-json output — Claude Code emits a final `result` event containing
    aggregated `usage` and `total_cost_usd`, so no proxy is needed.
    """
    print(f"  [CCR] Configuring model: {provider},{model}")

    # Update CCR config to use the requested model and restart
    orig_config = ccr_read_config()
    patched = json.loads(json.dumps(orig_config))
    patched.setdefault("Router", {})["default"] = f"{provider},{model}"
    ccr_write_config(patched)
    ccr_restart()

    # ccr code uses minimist internally which drops positional args (the prompt).
    # Pass the prompt via stdin instead.
    cmd = [
        "ccr", "code",
        "--dangerously-skip-permissions",
        "--allowedTools", "all",
        "--verbose",
        "--print",
        "--output-format", "stream-json",
    ]

    trace_file = output_dir / "ccr_trace.json"
    stdout_file = output_dir / "ccr_stdout.txt"
    stderr_file = output_dir / "ccr_stderr.txt"

    # Unset CLAUDECODE so claude doesn't refuse to run inside another session.
    # Set IS_SANDBOX=1 so --dangerously-skip-permissions works when running as root.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["IS_SANDBOX"] = "1"

    print(f"  [CCR] Running task (timeout={timeout}s) ...")
    t_start = time.time()
    try:
        with open(stdout_file, "w") as stdout_f, open(stderr_file, "w") as stderr_f:
            proc = subprocess.run(
                cmd,
                input=task.encode(),
                env=env,
                stdout=stdout_f,
                stderr=stderr_f,
                timeout=timeout,
            )
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        returncode = -1
        print("  [CCR] TIMEOUT")
    except Exception as e:
        returncode = -2
        print(f"  [CCR] ERROR: {e}")
    finally:
        elapsed = time.time() - t_start
        # Restore original config and restart CCR
        ccr_write_config(orig_config)
        try:
            ccr_restart()
        except Exception:
            pass

    # Parse stream-json stdout for all stats
    ccr_stats = _parse_ccr_stdout(stdout_file)

    response_file = output_dir / "ccr_response.txt"
    response_file.write_text(ccr_stats["response_text"])

    with open(trace_file, "w") as f:
        json.dump({
            "tool": "ccr",
            "model": f"{provider},{model}",
            "returncode": returncode,
            "elapsed_sec": elapsed,
            **ccr_stats,
        }, f, indent=2)

    pricing = load_pricing()
    price_entry = get_price(pricing, provider, model)
    # Prefer cost from claude's own reporting; fall back to pricing table calc
    cost = ccr_stats["cost_usd"] or calc_cost(price_entry, ccr_stats["tokens"])

    return {
        "tool": "ccr",
        "model": f"{provider},{model}",
        "returncode": returncode,
        "elapsed_sec": round(elapsed, 1),
        "api_calls": ccr_stats["api_calls"],
        "tool_calls": ccr_stats["tool_calls"],
        "tokens": ccr_stats["tokens"],
        "cost_usd": round(cost, 6),
        "price_per_1m": price_entry,
        "trace_file": str(trace_file),
        "response_file": str(response_file),
        "stdout_file": str(stdout_file),
        "stderr_file": str(stderr_file),
    }


def _parse_ccr_stdout(path: Path) -> dict:
    """
    Parse Claude Code --output-format stream-json output.

    The final `result` event contains aggregated usage and cost.
    Assistant messages contain tool_use blocks for counting tool calls.
    Returns a dict with: tokens, cost_usd, api_calls, tool_calls, response_text.
    """
    result = {
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
        "cost_usd": 0.0,
        "api_calls": 0,
        "tool_calls": 0,
        "response_text": "",
    }
    if not path.exists():
        return result

    text_parts: list[str] = []

    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue

            t = obj.get("type", "")

            # Final summary event — has aggregated usage + cost
            if t == "result":
                usage = obj.get("usage", {})
                result["tokens"] = {
                    "input": usage.get("input_tokens", 0),
                    "output": usage.get("output_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                    "cache_write": usage.get("cache_creation_input_tokens", 0),
                }
                result["cost_usd"] = obj.get("total_cost_usd", 0.0) or 0.0
                result["api_calls"] = obj.get("num_turns", 0)

            # Assistant messages: extract text and count tool_use blocks
            elif t == "assistant":
                content = obj.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        if block.get("type") == "tool_use":
                            result["tool_calls"] += 1
                        elif block.get("type") == "text":
                            text_parts.append(block.get("text", ""))

    result["response_text"] = "\n\n".join(t for t in text_parts if t)
    return result


def _aggregate_tokens(records: list) -> dict:
    """Aggregate token counts from proxy-captured usage records.

    Handles both Anthropic-style field names (input_tokens / output_tokens /
    cache_read_input_tokens / cache_creation_input_tokens) and OpenAI-style
    (prompt_tokens / completion_tokens) as returned by OpenRouter.
    """
    result = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for r in records:
        t = r.get("tokens", {})
        # Anthropic field names
        result["input"] += t.get("input_tokens", 0)
        result["output"] += t.get("output_tokens", 0)
        result["cache_read"] += t.get("cache_read_input_tokens", 0)
        result["cache_write"] += t.get("cache_creation_input_tokens", 0)
        # OpenAI / OpenRouter field names
        result["input"] += t.get("prompt_tokens", 0)
        result["output"] += t.get("completion_tokens", 0)
        # OpenRouter-specific cache fields
        result["cache_read"] += (
            t.get("prompt_tokens_details", {}) or {}
        ).get("cached_tokens", 0)
    return result


def run_opencode(task: str, provider: str, model: str, output_dir: Path, timeout: int) -> dict:
    """
    Run `opencode run` with the given task.
    Returns stats dict.
    """
    oc_model = model_for_opencode(provider, model)
    print(f"  [OpenCode] Running model: {oc_model}")

    trace_file = output_dir / "opencode_trace.json"
    stdout_file = output_dir / "opencode_stdout.txt"
    stderr_file = output_dir / "opencode_stderr.txt"

    # Note: opencode run --format json outputs JSON events to stdout
    cmd = [
        str(OPENCODE_BIN),
        "run",
        "--model", oc_model,
        "--format", "json",
        task,
    ]
    env = {**os.environ, "OPENCODE_PERMISSION": '{"*":"allow"}'}

    print(f"  [OpenCode] Running task (timeout={timeout}s) ...")
    t_start = time.time()
    session_id = None
    returncode = 0
    try:
        with open(stdout_file, "w") as stdout_f, open(stderr_file, "w") as stderr_f:
            proc = subprocess.run(
                cmd,
                env=env,
                stdout=stdout_f,
                stderr=stderr_f,
                timeout=timeout,
            )
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        returncode = -1
        print("  [OpenCode] TIMEOUT")
    except Exception as e:
        returncode = -2
        print(f"  [OpenCode] ERROR: {e}")
    finally:
        elapsed = time.time() - t_start

    # Parse stdout JSON events: extract session ID, errors, and response text
    session_id, run_errors, response_text = _parse_opencode_stdout(stdout_file)

    if run_errors and returncode == 0:
        # opencode exits 0 even on model-not-found errors; treat as failure
        returncode = -3
        for err in run_errors:
            print(f"  [OpenCode] Error from agent: {err}")

    # Query SQLite DB for stats
    db_stats = _query_opencode_db(session_id) if session_id else {}

    # Write human-readable response
    response_file = output_dir / "opencode_response.txt"
    response_file.write_text(response_text)

    # Save trace
    with open(trace_file, "w") as f:
        json.dump({
            "tool": "opencode",
            "model": oc_model,
            "session_id": session_id,
            "returncode": returncode,
            "elapsed_sec": elapsed,
            "errors": run_errors,
            "db_stats": db_stats,
        }, f, indent=2)

    total_tokens = db_stats.get("tokens", {})
    pricing = load_pricing()
    price_entry = get_price(pricing, provider, model)
    cost = calc_cost(price_entry, total_tokens)

    return {
        "tool": "opencode",
        "model": oc_model,
        "session_id": session_id,
        "returncode": returncode,
        "errors": run_errors,
        "elapsed_sec": round(elapsed, 1),
        "api_calls": db_stats.get("api_calls", 0),
        "tool_calls": db_stats.get("tool_calls", 0),
        "tokens": total_tokens,
        "cost_usd": round(cost, 6),
        "price_per_1m": price_entry,
        "trace_file": str(trace_file),
        "response_file": str(response_file),
        "stdout_file": str(stdout_file),
        "stderr_file": str(stderr_file),
    }


def _parse_opencode_stdout(stdout_file: Path) -> tuple[Optional[str], list[str], str]:
    """
    Parse opencode --format json stdout.
    Returns (session_id, list_of_error_messages, assistant_response_text).
    """
    if not stdout_file.exists():
        return None, [], ""

    session_id: Optional[str] = None
    errors: list[str] = []
    text_parts: list[str] = []

    with open(stdout_file) as f:
        content = f.read()

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue

        evt_type = obj.get("type", "")

        # Session ID appears in most event types
        sid = obj.get("sessionID") or obj.get("session_id")
        if not sid:
            sid = (obj.get("properties") or obj.get("session") or {}).get("id")
        if not sid and obj.get("id", "").startswith("ses_"):
            sid = obj["id"]
        if sid and str(sid).startswith("ses_") and not session_id:
            session_id = str(sid)

        # Collect streamed assistant text
        if evt_type == "text":
            text = (obj.get("part") or {}).get("text", "")
            if text:
                text_parts.append(text)

        # Capture errors
        if evt_type == "error":
            err_data = obj.get("error", {})
            msg = (
                err_data.get("data", {}).get("message")
                or err_data.get("message")
                or str(err_data)
            )
            errors.append(msg)

    # Last-resort: regex scan for ses_ token
    if not session_id:
        m = re.search(r'"(ses_[a-zA-Z0-9]+)"', content)
        if m:
            session_id = m.group(1)

    return session_id, errors, "".join(text_parts)


def _query_opencode_db(session_id: str) -> dict:
    """Query opencode SQLite DB for session stats."""
    if not OPENCODE_DB.exists():
        return {}
    try:
        conn = sqlite3.connect(str(OPENCODE_DB))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Token totals from assistant messages
        cur.execute("""
            SELECT
                SUM(json_extract(data, '$.tokens.input'))        AS input,
                SUM(json_extract(data, '$.tokens.output'))       AS output,
                SUM(json_extract(data, '$.tokens.cache.read'))   AS cache_read,
                SUM(json_extract(data, '$.tokens.cache.write'))  AS cache_write,
                COUNT(*)                                          AS api_calls
            FROM message
            WHERE session_id = ?
              AND json_extract(data, '$.role') = 'assistant'
        """, (session_id,))
        row = dict(cur.fetchone())

        # Tool call count
        cur.execute("""
            SELECT COUNT(*) as cnt
            FROM part
            WHERE session_id = ?
              AND json_extract(data, '$.type') = 'tool'
        """, (session_id,))
        tool_row = cur.fetchone()
        tool_calls = tool_row["cnt"] if tool_row else 0

        conn.close()

        return {
            "tokens": {
                "input": int(row.get("input") or 0),
                "output": int(row.get("output") or 0),
                "cache_read": int(row.get("cache_read") or 0),
                "cache_write": int(row.get("cache_write") or 0),
            },
            "api_calls": int(row.get("api_calls") or 0),
            "tool_calls": tool_calls,
        }
    except Exception as e:
        print(f"  [OpenCode] DB query error: {e}")
        return {}


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------
def print_stats(stats: dict):
    tokens = stats.get("tokens", {})
    errors = stats.get("errors", [])
    print()
    print("=" * 60)
    print(f"  Tool       : {stats['tool'].upper()}")
    print(f"  Model      : {stats['model']}")
    if stats.get("session_id"):
        print(f"  Session    : {stats['session_id']}")
    rc = stats['returncode']
    rc_label = {0: "OK", -1: "TIMEOUT", -2: "EXCEPTION", -3: "AGENT ERROR"}.get(rc, str(rc))
    print(f"  Exit code  : {rc} ({rc_label})")
    if errors:
        for e in errors:
            print(f"  ERROR      : {e}")
    print(f"  Elapsed    : {stats['elapsed_sec']}s")
    print(f"  API calls  : {stats.get('api_calls', '?')}")
    print(f"  Tool calls : {stats.get('tool_calls', '?')}")
    print(f"  Tokens     :")
    print(f"    Input         : {tokens.get('input', 0):>10,}")
    print(f"    Output        : {tokens.get('output', 0):>10,}")
    print(f"    Cache read    : {tokens.get('cache_read', 0):>10,}")
    print(f"    Cache write   : {tokens.get('cache_write', 0):>10,}")
    print(f"  Est. cost  : ${stats.get('cost_usd', 0):.6f}")
    if stats.get("price_per_1m"):
        p = stats["price_per_1m"]
        print(f"  Pricing    : input=${p.get('input')} output=${p.get('output')} /1M tokens")
    print(f"  Trace      : {stats['trace_file']}")
    print(f"  Response   : {stats['response_file']}")
    print(f"  Stdout     : {stats['stdout_file']}")
    print("=" * 60)


def save_summary(results: list, output_dir: Path):
    summary_file = output_dir / "summary.json"
    with open(summary_file, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nSummary saved to: {summary_file}")

    # Also print comparison table if multiple results
    if len(results) > 1:
        print()
        print("COMPARISON TABLE")
        print("-" * 80)
        hdr = f"{'Tool':<10} {'Model':<45} {'API':>5} {'Tools':>6} {'In':>8} {'Out':>8} {'$':>10}"
        print(hdr)
        print("-" * 80)
        for r in results:
            tok = r.get("tokens", {})
            print(
                f"{r['tool']:<10} {r['model']:<45} "
                f"{r.get('api_calls',0):>5} {r.get('tool_calls',0):>6} "
                f"{tok.get('input',0):>8,} {tok.get('output',0):>8,} "
                f"${r.get('cost_usd',0):>9.6f}"
            )
        print("-" * 80)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Benchmark token usage and cost for CCR and OpenCode."
    )
    parser.add_argument(
        "--tool", required=True,
        choices=["ccr", "opencode", "both"],
        help="Which coding agent to benchmark."
    )
    parser.add_argument(
        "--model", required=True,
        help=(
            "Model in 'provider/model' or 'provider,model' format. "
            "For OpenRouter: 'openrouter/anthropic/claude-sonnet-4-6'."
        ),
    )
    parser.add_argument(
        "--task", required=True,
        help="Path to task prompt file (e.g. tasks/example_task.txt).",
    )
    parser.add_argument(
        "--workdir",
        help="Working directory for the agent. Defaults to a temp dir.",
    )
    parser.add_argument(
        "--timeout", type=int, default=300,
        help="Timeout in seconds per run (default: 300).",
    )
    parser.add_argument(
        "--output-dir",
        help="Where to save results. Defaults to results/<timestamp>/.",
    )
    args = parser.parse_args()

    # Read task
    task_path = Path(args.task)
    if not task_path.exists():
        print(f"ERROR: Task file not found: {task_path}", file=sys.stderr)
        sys.exit(1)
    task_prompt = task_path.read_text().strip()

    # Parse model
    provider, model = parse_model(args.model)

    # Output dir
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tool_tag = args.tool
    model_tag = model.replace("/", "_").replace(",", "_")
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = RESULTS_DIR / f"{ts}_{tool_tag}_{model_tag}"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save task copy for reference
    (output_dir / "task.txt").write_text(task_prompt)

    print(f"\nBenchmark run")
    print(f"  Task    : {task_path}")
    print(f"  Model   : {provider}/{model}")
    print(f"  Tool(s) : {args.tool}")
    print(f"  Output  : {output_dir}")

    # Working directory: default to the task file's parent so relative paths
    # inside the prompt (e.g. ./tasks/search_task.txt) resolve correctly.
    if args.workdir:
        workdir = Path(args.workdir)
        workdir.mkdir(parents=True, exist_ok=True)
    else:
        workdir = task_path.resolve().parent
    tmp_created = False
    print(f"  Workdir : {workdir}")

    results = []

    if args.tool in ("ccr", "both"):
        print("\n--- CCR Run ---")
        orig_dir = os.getcwd()
        os.chdir(workdir)
        try:
            stats = run_ccr(task_prompt, provider, model, output_dir, args.timeout)
        finally:
            os.chdir(orig_dir)
        print_stats(stats)
        results.append(stats)

    if args.tool in ("opencode", "both"):
        print("\n--- OpenCode Run ---")
        orig_dir = os.getcwd()
        os.chdir(workdir)
        try:
            stats = run_opencode(task_prompt, provider, model, output_dir, args.timeout)
        finally:
            os.chdir(orig_dir)
        print_stats(stats)
        results.append(stats)

    save_summary(results, output_dir)


if __name__ == "__main__":
    main()
