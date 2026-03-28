import sys
from pathlib import Path

# Ensure kiro-agents server dir is first on sys.path so `import server` resolves correctly
sys.path.insert(0, str(Path(__file__).parent.parent))
