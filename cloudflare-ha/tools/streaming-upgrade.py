#!/usr/bin/env python3
"""CLI entry point: stage/status a dedicated PostgreSQL streaming cluster."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from streaming_setup import main

if __name__ == '__main__':
    main()
