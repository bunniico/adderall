"""The calendar's day-grouping, run through node.

One JavaScript test, not a suite. `groupByDay` decides what the week and
month views draw and its edge cases are all off-by-one, so it is worth
checking directly rather than by looking at a screenshot; the rest of
calendar.js is drawing code that a unit test would not say much about.

Wrapped in pytest so there is still one command that runs everything.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "group_by_day.mjs"


@pytest.mark.skipif(shutil.which("node") is None,
                    reason="node is not installed; the Python suite still covers the API")
def test_group_by_day_files_work_under_every_day_it_touches():
    done = subprocess.run(["node", str(HARNESS)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr or done.stdout
