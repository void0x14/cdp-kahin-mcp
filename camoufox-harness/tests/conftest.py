"""Path setup for camoufox-harness tests.

Makes the build-time tools importable as `protocol.extractor.extractor`
(camoufox-harness/protocol lives one level above this directory).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
