#!/usr/bin/env python3
"""RPA 启动器：支持以隔离模式运行（python -I run_rpa.py ...）。

隔离模式下 sys.path 不含当前目录，因此这里显式补入脚本所在目录。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rpa.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
