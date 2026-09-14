"""The two decisions in calendar.js, run through node.

Not a JavaScript suite — two functions. `groupByDay` decides what the week
and month views draw; `biggerThanADay` decides the ⚠ and the ⚡ on every
chip and block. Both turn on edges that no screenshot shows: a span ending
at midnight, two spans on one day, work with no spans at all, and whether
exactly a day counts as more than one. The rest of calendar.js is drawing
code a unit test would say little about.

Wrapped in pytest so there is still one command that runs everything.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "calendar_logic.mjs"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the Python suite still covers the API")
def test_calendar_grouping_and_size_warning():
    done = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr or done.stdout
