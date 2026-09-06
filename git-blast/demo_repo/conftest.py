"""Put the demo repo root on sys.path so `import src.*` resolves here."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
