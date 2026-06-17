import asyncio
import gc
import sys
import time

import pytest


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(autouse=True)
def release_windows_testclient_sockets():
    yield
    if sys.platform != "win32":
        return
    # Starlette/FastAPI TestClient creates short-lived loop sockets on Windows.
    # Force cleanup between tests so full-suite runs do not hit WinError 10055.
    gc.collect()
    time.sleep(0.02)
