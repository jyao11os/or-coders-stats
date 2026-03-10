# or-coders-stats

Benchmark token usage, cost, and tool-call statistics for OpenRouter-powered coding agents — [Claude Code Router (CCR)](https://github.com/musistudio/claude-code-router) and [OpenCode](https://opencode.ai) — across different models.

## Overview

`bench.py` runs a task prompt through one or both coding agents, captures all API interactions, and writes per-run statistics to a timestamped results directory. For each run you get:

| File | Contents |
|------|----------|
| `*_stdout.txt` | Raw agent output (stream-json for CCR, JSON events for OpenCode) |
| `*_stderr.txt` | Agent stderr |
| `*_response.txt` | Final assistant text response only |
| `*_full_execution_sequence.txt` | Human-readable log of every assistant message, tool call, and tool result |
| `*_trace.json` | Aggregated stats: tokens, cost, API calls, tool calls, elapsed time |
| `summary.json` | Combined stats for all tools run in this invocation |

## Requirements

- **CCR**: [claude-code-router](https://github.com/musistudio/claude-code-router) installed and configured (`~/.claude-code-router/config.json` with an OpenRouter provider and API key)
- **OpenCode**: `opencode` binary available at `~/.opencode/bin/opencode`
- **Python 3.10+** (stdlib only, no extra dependencies)
- **Pricing data**: `models-pricing.json` in the project root (USD per 1M tokens, keyed by provider/model)

## Usage

```bash
python bench.py --tool <ccr|opencode|both> --model <provider>/<model> --task <task_file> [--timeout <seconds>]
```

The `--model` argument accepts either slash or comma as separator:

```bash
# CCR only
python bench.py --tool ccr --model openrouter/anthropic/claude-sonnet-4-6 --task tasks/example_task.txt

# OpenCode only
python bench.py --tool opencode --model openrouter/moonshotai/kimi-k2.5 --task tasks/example_task.txt

# Both agents, custom timeout
python bench.py --tool both --model openrouter/minimax/minimax-m2.5 --task tasks/ccr-task.txt --timeout 900
```

Results are saved to `results/<timestamp>_<tool>_<model>/`.

## Regenerating execution sequences

To regenerate `*_full_execution_sequence.txt` for earlier results without re-running the agent:

```bash
# Single stdout file
python gen_execution_sequence.py results/20260306_141257_ccr_moonshotai_kimi-k2.5/ccr_stdout.txt

# Entire result directory (auto-detects ccr/opencode)
python gen_execution_sequence.py results/20260306_141257_ccr_moonshotai_kimi-k2.5/

# All result directories at once
python gen_execution_sequence.py results/
```

## CCR model-specific configuration

Some models require transformer overrides in `~/.claude-code-router/config.json`. Known cases:

- **MiniMax M2.5** — requires reasoning to be enabled. The `customparams` transformer is used to force `reasoning.enabled = true` after the Anthropic→OpenAI body conversion (the `reasoning` transformer acts too early and doesn't work here). Add to the provider's `transformer` block:
  ```json
  "minimax/minimax-m2.5": {
    "use": ["openrouter", ["customparams", {"reasoning": {"enabled": true}}]]
  }
  ```
