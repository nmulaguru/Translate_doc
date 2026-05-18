"""Sandbox policy must block the obvious escape vectors.

This is the script-side defense layer. `python -I` and the filtered-builtins
dict in `runner.py` are the second layer. Together they keep a misbehaving
LLM-generated script from reaching the host filesystem or subprocess.
"""

import pytest

from app.sandbox.policy import PolicyViolation, check_script


def test_allowed_imports_pass():
    src = """
import json
import asyncio
import math
from collections import Counter
from datetime import datetime
import mcp

async def main():
    pass
asyncio.run(main())
"""
    check_script(src)  # should not raise


def test_blocked_os_import():
    with pytest.raises(PolicyViolation):
        check_script("import os")


def test_blocked_subprocess_import():
    with pytest.raises(PolicyViolation):
        check_script("import subprocess")


def test_blocked_from_socket_import():
    with pytest.raises(PolicyViolation):
        check_script("from socket import socket")


def test_blocked_dunder_import_call():
    with pytest.raises(PolicyViolation):
        check_script("__import__('os')")


def test_blocked_eval_call():
    with pytest.raises(PolicyViolation):
        check_script("eval('1+1')")


def test_blocked_exec_call():
    with pytest.raises(PolicyViolation):
        check_script("exec('print(1)')")


def test_blocked_dunder_subclasses():
    with pytest.raises(PolicyViolation):
        check_script("().__class__.__bases__")


def test_blocked_open():
    with pytest.raises(PolicyViolation):
        check_script("open('secret.txt')")


def test_syntax_error_is_violation():
    with pytest.raises(PolicyViolation):
        check_script("def )(:")
