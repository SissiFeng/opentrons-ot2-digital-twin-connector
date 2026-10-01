#!/usr/bin/env python3
"""Capture the activated native Isaac environment without installing bridge into it."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ot2_bridge.site_setup import main

main()
