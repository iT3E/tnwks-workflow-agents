"""In place of github_app_token.py of tnwks-ops: the test token from the env, on standard output."""

import os
import sys

sys.stdout.write(os.environ["FAKE_GITHUB_TOKEN"])
