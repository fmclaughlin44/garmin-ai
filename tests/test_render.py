"""
Executes the built dashboard end to end and asserts the page actually draws.

Every other test here checks the pipeline's arithmetic. This one checks the
thing arithmetic can't: that the page built from that data renders. A syntax
check happily passes a page that renders nothing, which is exactly what
happened when two elements shared an id -- one renderer wiped its own section,
every section after it stopped drawing, and no tool said a word.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import build_dashboard  # noqa: E402

RUNNER = ROOT / "tests" / "render_check" / "run.js"


def test_template_has_no_duplicate_ids_or_dangling_references():
    """The static half of the check — runs without node."""
    problems = build_dashboard.validate((ROOT / "web" / "template.html").read_text())
    assert problems == [], "; ".join(problems)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_built_page_renders_and_every_mount_populates(tmp_path):
    data_path = ROOT / "web" / "demo-data.json"
    if not data_path.exists():
        pytest.skip("run make_demo_data.py first")

    out = tmp_path / "dashboard.html"
    html = build_dashboard.render(
        (ROOT / "web" / "template.html").read_text(),
        json.loads(data_path.read_text()),
    )
    out.write_text(html)

    result = subprocess.run([shutil.which("node"), str(RUNNER), str(out)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_a_duplicate_id_is_caught_before_it_ships():
    """The regression that started this: a section and a mount sharing an id."""
    template = (ROOT / "web" / "template.html").read_text()
    broken = template.replace('id="block-weeks"', 'id="block"')
    problems = build_dashboard.validate(broken)
    assert any("duplicate" in p for p in problems)
