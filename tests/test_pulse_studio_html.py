"""
Syntax-check every inline <script> in pulse_studio.html with Node, so a broken edit
can't silently kill the whole UI. Skipped when Node isn't installed.
"""
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_inline_scripts_parse(tmp_path):
    html = open(os.path.join(ROOT, "pulse_studio.html"), encoding="utf-8").read()
    blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert blocks, "no inline scripts found"
    for i, code in enumerate(blocks):
        f = tmp_path / f"block{i}.js"
        f.write_text(code, encoding="utf-8")
        res = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert res.returncode == 0, f"<script> #{i} has a syntax error:\n{res.stderr}"
