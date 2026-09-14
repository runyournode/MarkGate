"""POST /images: auth, request validation per backend, response shape. S3 access is stubbed."""

import base64

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import api
from config.settings import settings

SHA = "0123456789abcdef" * 4
KEY = "images-key"
IMG = "imgs/6f715daebc26828f.jpg"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "client_api_key_images", KEY)
    monkeypatch.setattr(api, "s3_is_active", lambda: True)
    calls = []

    async def fake_get_images_by_name(file_hash, config, names):
        calls.append((file_hash, config.backend_type, names))
        return {IMG: b"jpeg-bytes"}, [name for name in names if name != IMG]

    monkeypatch.setattr(api, "get_images_by_name", fake_get_images_by_name)
    app = FastAPI()
    app.include_router(api.router)
    test_client = TestClient(app)
    test_client.calls = calls  # type: ignore[attr-defined]
    return test_client


def post(client, body, key=KEY):
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    return client.post("/images", json=body, headers=headers)


def body(**overrides):
    return {"engine": "foil", "source_sha256": SHA, "names": [IMG], **overrides}


class TestAuth:
    def test_missing_key(self, client):
        assert post(client, body(), key=None).status_code == 403

    def test_wrong_key(self, client):
        assert post(client, body(), key="nope").status_code == 403

    def test_unset_key_rejects_everything(self, client, monkeypatch):
        monkeypatch.setattr(settings, "client_api_key_images", "")
        assert post(client, body(), key="").status_code == 403
        assert post(client, body(), key="anything").status_code == 403


class TestValidation:
    @pytest.mark.parametrize(
        "payload",
        [
            {"engine": "foil", "source_sha256": SHA},  # names required
            body(names=[]),
            body(names=[f"{i:016x}.jpg" for i in range(51)]),
            body(source_sha256="xyz"),
            body(extra="field"),
        ],
    )
    def test_invalid_body(self, client, payload):
        assert post(client, payload).status_code == 422

    @pytest.mark.parametrize(
        "name", ["imgs/../6f715daebc26828f.jpg", "a/6f715daebc26828f.jpg", "x.jpg"]
    )
    def test_invalid_image_name_for_engine(self, client, name):
        response = post(client, body(names=[name]))
        assert response.status_code == 422
        assert client.calls == []

    @pytest.mark.parametrize("engine", ["unknown", "xberg"])
    def test_engine_without_image_lookup(self, client, engine):
        response = post(client, body(engine=engine))
        assert response.status_code == 422
        assert "does not support image lookup" in response.json()["detail"]

    def test_s3_unavailable(self, client, monkeypatch):
        monkeypatch.setattr(api, "s3_is_active", lambda: False)
        assert post(client, body()).status_code == 503


class TestResponse:
    def test_found_and_missing_with_optional_folder_prefix(self, client):
        other = "0a1b2c3d4e5f6a7b.jpg"
        response = post(client, body(names=["6f715daebc26828f.jpg", other, IMG]))

        assert response.status_code == 200
        assert client.calls == [
            (SHA, "foil", [IMG, f"imgs/{other}"])
        ]  # normalized, deduplicated
        payload = response.json()
        assert payload["images"] == {
            IMG: {
                "mime": "image/jpeg",
                "data": base64.b64encode(b"jpeg-bytes").decode(),
            }
        }
        assert payload["missing"] == [f"imgs/{other}"]
