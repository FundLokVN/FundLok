"""Make tools/mcp-servers/fl-knowledge importable. These are offline unit
tests: no Pinecone, no embedding model, no network."""
import sys
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parents[2] / "tools" / "mcp-servers" / "fl-knowledge"
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))
