from fastapi.testclient import TestClient

from dualgent.config import Settings
from dualgent.server import create_app


def test_api_requires_token_correct_host_and_same_origin(tmp_path):
    app = create_app(Settings(tmp_path, "fake-key"), token="test-token")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/").status_code == 200
        assert client.get("/api/tasks").status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        assert client.get("/api/tasks", headers=headers).status_code == 200
        assert (
            client.get("/api/tasks", headers={**headers, "Origin": "https://evil.test"}).status_code
            == 403
        )
        assert client.get("/api/tasks", headers={**headers, "Host": "evil.test"}).status_code == 403
        status = client.get("/api/status", headers=headers)
        assert "fake-key" not in status.text
        assert client.get("/assets/connection-token").status_code == 404


def test_extension_requires_origin_and_pairing_token(tmp_path):
    app = create_app(Settings(tmp_path, "fake-key"), token="test-token")
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        import pytest
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "ws://127.0.0.1:8765/extension", headers={"Origin": "https://evil.test"}
            ):
                pass
        headers = {"Origin": "chrome-extension://" + "a" * 32}
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(
                "ws://127.0.0.1:8765/extension", headers=headers
            ) as socket:
                socket.send_json({"type": "hello", "token": "wrong", "instance": "a" * 32})
                socket.receive_json()
        with client.websocket_connect("ws://127.0.0.1:8765/extension", headers=headers) as socket:
            socket.send_json(
                {"type": "hello", "token": "test-token", "instance": "a" * 32, "name": "Test"}
            )
            assert socket.receive_json() == {"type": "connected"}
            status = client.get(
                "/api/status", headers={"Authorization": "Bearer test-token"}
            ).json()
            assert status["browsers"] == [{"id": "a" * 32, "name": "Test"}]
