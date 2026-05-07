"""Reference script for subprocess timeout tests.

This file is NOT executed by the test suite. The subprocess timeout behaviour is
verified via ``unittest.mock.patch`` in ``test_provider_contract_subprocess.py`` so we do not
actually block a worker for 60 seconds. The script is kept as documentation of
what a "hangs for a long time" command would look like.
"""

import time

if __name__ == "__main__":
    time.sleep(60)
