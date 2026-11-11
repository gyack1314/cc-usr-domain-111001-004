#!/usr/bin/env python3
"""本地门户模拟器启动器：支持隔离模式（python -I run_source.py --port 8090）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from source_site.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
