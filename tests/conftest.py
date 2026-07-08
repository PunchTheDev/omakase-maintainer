import socket
import time
import threading

import pytest

from omakase_eval import mockpool


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_ready(port: int, tries: int = 100) -> None:
    for _ in range(tries):
        if _port_open(port):
            return
        time.sleep(0.02)


@pytest.fixture(scope="session")
def pool_server():
    """Serve the mock pool on 8100, or reuse one already running there."""
    if _port_open(8100):
        yield "http://127.0.0.1:8100"  # an external pool is already up
        return
    server = mockpool.serve(port=8100)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    _wait_ready(8100)  # don't yield until the server accepts connections
    yield "http://127.0.0.1:8100"
    server.shutdown()
