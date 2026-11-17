import os
import sys
import tempfile
from pathlib import Path

# Settings are read at import, so set the environment first.
_tmp = tempfile.mkdtemp(prefix="agentry-test-")
os.environ["AGENTRY_DATA"] = _tmp
os.environ["AGENTRY_ADMIN_PASSWORD"] = "test-admin-pw"
os.environ["AGENTRY_SCHEDULER_TICK"] = "3600"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
