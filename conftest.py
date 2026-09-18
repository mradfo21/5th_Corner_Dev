"""pytest's entry into the authoring sandbox. The sandbox itself is in
``authoring_sandbox.py``.

This file used to BE the sandbox, which made the protection depend on the runner:
every test module documents itself as ``python3 -m unittest <module> -v``, and
unittest never loads conftest.py. So the documented way to run the tests was the
one way the guard could not see, and a full unittest run wrote over the live
prompt file, ``worlds/world.json`` and ``tunables.json``.

The guard now engages on import of any authoring store — see
``authoring_sandbox.guard`` — so by the time pytest gets here it is usually
already on. This fixture stays for the one thing it adds: an explicit release at
the end of the session, so a pytest run inside a longer-lived process puts the
real paths back rather than waiting for interpreter exit.
"""
from __future__ import annotations

import pytest

import authoring_sandbox

ROOT = authoring_sandbox.ROOT


@pytest.fixture(scope="session", autouse=True)
def _authoring_data_is_a_copy():
    sandbox = authoring_sandbox.engage("pytest session")
    yield sandbox
    authoring_sandbox.release()
