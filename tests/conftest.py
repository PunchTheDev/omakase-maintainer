import socket
import threading

import pytest

from oc_eval import mockpool


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture(scope="session")
def pool_server():
    """Serve the mock pool on 8100, or reuse one already running there."""
    if _port_open(8100):
        yield "http://127.0.0.1:8100"  # an external pool is already up
        return
    server = mockpool.serve(port=8100)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:8100"
    server.shutdown()
