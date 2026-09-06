"""Make the ``src`` package importable when tests run from the package root."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
