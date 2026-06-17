import asyncio
import contextlib
import gc
import sys
import time
import weakref

import pytest


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

_OPEN_TEST_CLIENTS = weakref.WeakSet()


try:
    import fastapi.testclient as fastapi_testclient
except Exception:  # pragma: no cover - optional test dependency import guard
    fastapi_testclient = None


if fastapi_testclient is not None:
    _BaseTestClient = fastapi_testclient.TestClient

    class ReusablePortalTestClient(_BaseTestClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._ecse_managed_portal = False
            _OPEN_TEST_CLIENTS.add(self)

        def __enter__(self):
            if self.portal is None:
                super().__enter__()
                self._ecse_managed_portal = True
            return self

        def __exit__(self, *args):
            if self.portal is not None and getattr(self, "exit_stack", None) is not None:
                try:
                    return super().__exit__(*args)
                finally:
                    self._ecse_managed_portal = False
                    self.exit_stack = None
            self._ecse_managed_portal = False
            return None

        @contextlib.contextmanager
        def _portal_factory(self):
            if self.portal is None:
                self.__enter__()
            yield self.portal

        def close(self):
            self.__exit__(None, None, None)
            return super().close()

    fastapi_testclient.TestClient = ReusablePortalTestClient


@pytest.fixture(autouse=True)
def release_windows_testclient_sockets():
    yield
    for client in list(_OPEN_TEST_CLIENTS):
        with contextlib.suppress(Exception):
            client.close()
    if sys.platform != "win32":
        return
    # Starlette/FastAPI TestClient creates short-lived loop sockets on Windows.
    # Force cleanup between tests so full-suite runs do not hit WinError 10055.
    gc.collect()
    time.sleep(0.02)
