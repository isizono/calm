"""embedding_server 起動順序のテスト。

多重起動の敗者判定（bind 失敗 → exit）をモデルロードより前に走らせるため、
main() は bind → モデルロードの順で進む必要がある（並行 spawn 時に敗者が
モデルロード分のメモリを重複確保しないための順序）。
"""
import socket

import pytest

from src.infra import embedding_server
from src.infra.lock_file import is_port_listening


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def test_main_binds_port_before_model_load(monkeypatch):
    """main(): モデルロード時点でポートが既に bind されている"""
    port = _find_free_port()
    created = {}

    class RecordingServer(embedding_server.EmbeddingHTTPServer):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created["server"] = self

    load_observed = {}

    def fake_load():
        load_observed["port_bound"] = is_port_listening(port)
        raise SystemExit(0)  # serve_forever に進まず main を打ち切る

    monkeypatch.setattr(embedding_server, "PORT", port)
    monkeypatch.setattr(embedding_server, "_setup_logging", lambda: None)
    monkeypatch.setattr(embedding_server, "_load_model", fake_load)
    monkeypatch.setattr(embedding_server, "EmbeddingHTTPServer", RecordingServer)

    try:
        with pytest.raises(SystemExit):
            embedding_server.main()
    finally:
        if "server" in created:
            created["server"].server_close()

    assert load_observed["port_bound"] is True
