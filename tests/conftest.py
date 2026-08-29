# -*- coding: utf-8 -*-
"""
tests/conftest.py: Root Pytest Configuration and Global Fixtures
Pre-provisioned by Human Supervisor / Orchestrator
"""

import asyncio
import pytest

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test session."""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()
