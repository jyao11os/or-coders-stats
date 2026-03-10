#!/usr/bin/env python3
"""
gen_execution_sequence.py — regenerate *_full_execution_sequence.txt for earlier results.

Usage:
    # From a stdout file directly:
    python gen_execution_sequence.py results/20260306_141257_ccr_moonshotai_kimi-k2.5/ccr_stdout.txt

    # From a result directory (auto-detects ccr_stdout.txt / opencode_stdout.txt):
    python gen_execution_sequence.py results/20260306_141257_ccr_moonshotai_kimi-k2.5

    # Regenerate all result directories at once:
    python gen_execution_sequence.py results/
"""

import argparse
import sys
from pathlib import Path

# Import writers from bench.py without triggering its __main__ block
import importlib.util
spec = importlib.util.spec_from_file_location(
    "bench", Path(__file__).parent / "bench.py"
)
_bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_bench)
_write_ccr = _bench._write_ccr_execution_sequence
_write_opencode = _bench._write_opencode_execution_sequence


def process(path: Path) -> list[str]:
    """
    Process a single stdout file or a result directory.
    Returns a list of messages describing what was done.
    """
    msgs = []

    if path.is_file():
        name = path.name
        if name == "ccr_stdout.txt":
            out = path.parent / "ccr_full_execution_sequence.txt"
            _write_ccr(path, out)
            msgs.append(f"  wrote {out}")
        elif name == "opencode_stdout.txt":
            out = path.parent / "opencode_full_execution_sequence.txt"
            _write_opencode(path, out)
            msgs.append(f"  wrote {out}")
        else:
            msgs.append(f"  skipped {path} (not a recognised stdout file)")

    elif path.is_dir():
        found = False
        for stdout_file, writer, out_name in [
            ("ccr_stdout.txt",      _write_ccr,      "ccr_full_execution_sequence.txt"),
            ("opencode_stdout.txt", _write_opencode, "opencode_full_execution_sequence.txt"),
        ]:
            src = path / stdout_file
            if src.exists():
                out = path / out_name
                writer(src, out)
                msgs.append(f"  wrote {out}")
                found = True
        if not found:
            msgs.append(f"  skipped {path} (no ccr_stdout.txt or opencode_stdout.txt)")

    else:
        msgs.append(f"  skipped {path} (does not exist)")

    return msgs


def main():
    parser = argparse.ArgumentParser(
        description="Regenerate *_full_execution_sequence.txt from saved stdout files."
    )
    parser.add_argument(
        "input",
        help=(
            "A stdout file (ccr_stdout.txt / opencode_stdout.txt), "
            "a result directory, or the top-level results/ directory "
            "to process all subdirectories at once."
        ),
    )
    args = parser.parse_args()

    target = Path(args.input)

    # If the target is the top-level results dir, iterate its immediate subdirs
    if target.is_dir() and not (target / "ccr_stdout.txt").exists() and not (target / "opencode_stdout.txt").exists():
        subdirs = sorted(p for p in target.iterdir() if p.is_dir())
        if not subdirs:
            print(f"No subdirectories found in {target}", file=sys.stderr)
            sys.exit(1)
        for sub in subdirs:
            msgs = process(sub)
            if any("wrote" in m for m in msgs):
                print(sub.name)
                for m in msgs:
                    print(m)
    else:
        msgs = process(target)
        for m in msgs:
            print(m)
        if not any("wrote" in m for m in msgs):
            sys.exit(1)


if __name__ == "__main__":
    main()
