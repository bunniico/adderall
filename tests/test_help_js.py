"""The help renderer in help.js, run through node.

The articles under app/help/ are Markdown, and the page draws them with a
small renderer of its own; this checks it on each shape the articles use,
and that nothing in an article can run as HTML. Wrapped in pytest so there
is still one command that runs everything.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "help_markdown.mjs"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the Python suite still covers the API")
def test_help_markdown_renderer():
    done = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr or done.stdout
