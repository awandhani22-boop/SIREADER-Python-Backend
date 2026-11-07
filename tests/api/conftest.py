"""Konfigurasi pytest fixtures untuk test suite API Gateway (tests/api/)."""

import os
import sys

# Memastikan root project C:\SIREADER berada pada sys.path untuk test runner
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
