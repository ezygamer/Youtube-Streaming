import sys
from pathlib import Path

# Make `core` and `games` importable when running pytest from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
