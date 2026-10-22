"""Test setup that has to happen before any test module is imported.

The app reads UPLOADS_DIR once, when backend.app.main (through the processor) is first imported. A
test module that imports main early would otherwise decide for all the others - and an upload test
would write into the developer's real uploads/ folder. So the folder is fixed here, to a temporary
one, and removed when the run ends.
"""

import atexit
import os
import shutil
import tempfile

_UPLOADS = tempfile.mkdtemp(prefix="uploads-test-")
os.environ["UPLOADS_DIR"] = _UPLOADS
atexit.register(shutil.rmtree, _UPLOADS, ignore_errors=True)
