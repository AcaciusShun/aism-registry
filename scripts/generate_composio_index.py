#!/usr/bin/env python3
"""Backward-compatible wrapper for generating the Composio-only registry index."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> int:
    generator = Path(__file__).resolve().with_name("generate_index.py")
    cmd = [sys.executable, str(generator), "--source", "composiohq", *sys.argv[1:]]
    return subprocess.run(cmd, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
