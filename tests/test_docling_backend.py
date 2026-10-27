"""Tests for backends/docling.py — explicit-only query params, and the asynchronous
submit / poll / result sequence against docling-serve (mocked with httpx.MockTransport)."""

import asyncio
import base64
import hashlib

import httpx
import pytest
from pydantic import ValidationError

from backends import docling
from backends.docling import DoclingConfig, DoclingQueryParams, extract_embedded_images
from schemas import ProcessedDocument

PDF_MIME = "application/pdf"
BASE = "http://docling-serve:5001"


def make_docling_config(
    max_wait_s: float = 3600, **query_params_kwargs
) -> DoclingConfig:
    return DoclingConfig(
        backend_type="docling",
        description="test",
        upstream_url=BASE,
        authorized_api_key="key",
        poll_interval_s=0,
        max_wait_s=max_wait_s,
        query_params=DoclingQueryParams(**query_params_kwargs),
    )


class TestDoclingQueryParams:
    def test_nothing_sent_unless_declared(self):
        # No implicit value: docling-serve's own defaults apply to whatever the config omits.
        assert make_docling_config().get_raw_query_params() == {}

    def test_only_declared_params_are_sent(self):
        config = make_docling_config(
            pipeline="vlm", vlm_pipeline_preset="dots_mocr", do_picture_description=True
        )
        assert config.get_raw_query_params() == {
            "pipeline": "vlm",
            "vlm_pipeline_preset": "dots_mocr",
            "do_picture_description": True,
        }

    def test_unknown_param_rejected(self):
        with pytest.raises(ValidationError):
            DoclingQueryParams.model_validate({"ocr_engine": "tesseract"})

    def test_poll_settings_required(self):
        with pytest.raises(ValidationError):
            DoclingConfig.model_validate(
                {
                    "backend_type": "docling",
                    "description": "test",
                    "upstream_url": BASE,
                    "authorized_api_key": "key",
                }
            )


def run_call(config: DoclingConfig, handler) -> ProcessedDocument:
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await docling.call(
                config, b"%PDF", {"Content-Type": PDF_MIME}, "f.pdf", client
            )

    return asyncio.run(run())


class TestAsyncCall:
    def test_submit_poll_then_result(self):
        statuses = iter(["pending", "started", "success"])
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(f"{request.method} {request.url.path}")
            if request.url.path == "/v1/convert/file/async":
                body = request.content.decode(errors="ignore")
                assert 'name="pipeline"' in body and "standard" in body
                assert 'name="files"; filename="f.pdf"' in body
                return httpx.Response(
                    200, json={"task_id": "t1", "task_status": "pending"}
                )
            if request.url.path == "/v1/status/poll/t1":
                return httpx.Response(
                    200, json={"task_id": "t1", "task_status": next(statuses)}
                )
            if request.url.path == "/v1/result/t1":
                return httpx.Response(
                    200,
                    json={
                        "document": {"md_content": "# Title"},
                        "status": "success",
                        "processing_time": 1.5,
                        "errors": [],
                    },
                )
            return httpx.Response(404)

        document = run_call(make_docling_config(pipeline="standard"), handler)
        assert document.page_content == "# Title"
        assert document.metadata is not None
        assert document.metadata.root["status"] == "success"
        assert seen == [
            "POST /v1/convert/file/async",
            "GET /v1/status/poll/t1",
            "GET /v1/status/poll/t1",
            "GET /v1/status/poll/t1",
            "GET /v1/result/t1",
        ]

    def test_list_values_sent_as_repeated_fields(self):
        # docling rejects a JSON string ('["md"]') for to_formats: one field per item.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/convert/file/async":
                body = request.content.decode(errors="ignore")
                assert body.count('name="to_formats"') == 2
                assert '["md"' not in body
                return httpx.Response(200, json={"task_id": "t1"})
            if request.url.path == "/v1/status/poll/t1":
                return httpx.Response(200, json={"task_status": "success"})
            return httpx.Response(200, json={"document": {"md_content": "x"}})

        run_call(make_docling_config(to_formats=["md", "json"]), handler)

    def test_failure_raises(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/convert/file/async":
                return httpx.Response(200, json={"task_id": "t1"})
            return httpx.Response(200, json={"task_id": "t1", "task_status": "failure"})

        with pytest.raises(ValueError, match="failed"):
            run_call(make_docling_config(), handler)

    def test_gives_up_after_max_wait(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/convert/file/async":
                return httpx.Response(200, json={"task_id": "t1"})
            return httpx.Response(200, json={"task_id": "t1", "task_status": "started"})

        with pytest.raises(TimeoutError):
            run_call(make_docling_config(max_wait_s=0), handler)

    def test_submit_http_error_propagates(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"detail": "bad option"})

        with pytest.raises(httpx.HTTPStatusError):
            run_call(make_docling_config(), handler)


PNG = b"\x89PNG\r\n\x1a\n fake png"
JPEG = b"\xff\xd8\xff\xe0 fake jpeg \xff\xd9"


def data_uri(mime: str, data: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


class TestEmbeddedImages:
    def test_images_extracted_and_named_after_content(self):
        md = (
            f"Intro\n\n![Image]({data_uri('image/png', PNG)})\n\nA spring.\n\n"
            f"![Image]({data_uri('image/jpeg', JPEG)})\n"
        )
        page, images = extract_embedded_images(md)
        png_name = f"imgs/{hashlib.sha256(PNG).hexdigest()[:16]}.png"
        jpg_name = f"imgs/{hashlib.sha256(JPEG).hexdigest()[:16]}.jpg"
        assert images == {png_name: PNG, jpg_name: JPEG}
        assert (
            page
            == f"Intro\n\n![Image]({png_name})\n\nA spring.\n\n![Image]({jpg_name})\n"
        )

    def test_same_image_twice_stored_once(self):
        uri = data_uri("image/png", PNG)
        page, images = extract_embedded_images(f"![a]({uri}) ![b]({uri})")
        [name] = images
        assert page == f"![a]({name}) ![b]({name})"

    def test_markdown_without_images_unchanged(self):
        assert extract_embedded_images("# Title\n\n<!-- image -->") == (
            "# Title\n\n<!-- image -->",
            {},
        )

    def test_call_returns_extracted_images(self):
        md = f"![Image]({data_uri('image/png', PNG)})"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/convert/file/async":
                return httpx.Response(200, json={"task_id": "t1"})
            if request.url.path == "/v1/status/poll/t1":
                return httpx.Response(200, json={"task_status": "success"})
            return httpx.Response(200, json={"document": {"md_content": md}})

        document = run_call(make_docling_config(image_export_mode="embedded"), handler)
        [name] = document.images
        assert document.page_content == f"![Image]({name})"
        assert document.images[name] == PNG


class TestNormalizeImageName:
    SHA16 = "6f715daebc26828f"

    @pytest.mark.parametrize(
        "name", [f"{SHA16}.png", f"imgs/{SHA16}.png", f"imgs/{SHA16}.jpg"]
    )
    def test_valid_names(self, name):
        assert (
            DoclingConfig.normalize_image_name(name)
            == f"imgs/{name.removeprefix('imgs/')}"
        )

    @pytest.mark.parametrize(
        "name", [f"imgs/../{SHA16}.png", f"{SHA16}.gif", "x.png", f"a/{SHA16}.png"]
    )
    def test_invalid_names(self, name):
        assert DoclingConfig.normalize_image_name(name) is None

    def test_images_shared_across_processings(self):
        assert DoclingConfig.content_addressed_images is True
