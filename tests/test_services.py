"""Tests for services.call_upstream_backend's extraction_engine stamping — the single place
every backend's result passes through, so this is the only place that needs covering (not each
backends/*.py module) — and for services.save_failed_request's error.json."""

import asyncio
import json
from enum import Enum

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException

import services
from backends.utils import UpstreamProcessingError
from backends.foil import FoilConfig, FoilQueryParams
from schemas import ExternalDocumentRequestHeaders, Metadata, ProcessedDocument


def make_foil_config() -> FoilConfig:
    return FoilConfig(
        backend_type="foil",
        description="test",
        upstream_url="http://foil-serve:8081/v1/process",
        authorized_api_key="key",
        query_params=FoilQueryParams(),
    )


class TestCallUpstreamBackendStampsExtractionEngine:
    async def _fake_handler(self, config, file_content, headers, filename, client):
        return ProcessedDocument(
            page_content="hello",
            metadata=Metadata({"status": "ok"}),
        )

    async def _fake_handler_no_metadata(
        self, config, file_content, headers, filename, client
    ):
        return ProcessedDocument(page_content="hello")

    def test_stamps_backend_type_without_clobbering_existing_metadata(
        self, monkeypatch
    ):
        monkeypatch.setitem(services.BACKEND_HANDLERS, "foil", self._fake_handler)
        result = asyncio.run(
            services.call_upstream_backend(make_foil_config(), b"x", {}, "f.pdf")
        )
        assert result.metadata.root == {"status": "ok", "extraction_engine": "foil"}

    def test_stamps_backend_type_when_handler_returns_no_metadata(self, monkeypatch):
        monkeypatch.setitem(
            services.BACKEND_HANDLERS, "foil", self._fake_handler_no_metadata
        )
        result = asyncio.run(
            services.call_upstream_backend(make_foil_config(), b"x", {}, "f.pdf")
        )
        assert result.metadata.root == {"extraction_engine": "foil"}


class TestSaveFailedRequest:
    @staticmethod
    def _saved_error_json(monkeypatch, tmp_path, error: Exception) -> dict:
        # Local fallback (S3 off): same FailedRequestInfo as the one written to S3.
        monkeypatch.setattr(services, "s3_is_active", lambda: False)
        monkeypatch.setattr(
            services.settings, "failed_requests_local_dir", str(tmp_path)
        )
        asyncio.run(
            services.save_failed_request(
                file_content=b"%PDF-1.4",
                filename="f.pdf",
                file_hash="ab" * 32,
                version=Enum("V", {"V1": "v1"}).V1,
                error=error,
                upstream_duration_ms=1.0,
            )
        )
        (error_json,) = tmp_path.rglob("error.json")
        return json.loads(error_json.read_text())

    def test_backend_error_payload_saved(self, monkeypatch, tmp_path):
        details = {"status": "failure", "errors": [{"error_message": "boom"}]}
        saved = self._saved_error_json(
            monkeypatch, tmp_path, UpstreamProcessingError("docling failed", details)
        )
        assert saved["error_type"] == "UpstreamProcessingError"
        assert saved["error_message"] == "docling failed"
        assert saved["upstream_details"] == details
        assert saved["client_status_code"] == 502
        assert saved["client_message"].startswith("[v1] Processing failed for 'f.pdf'")

    def test_http_error_body_saved(self, monkeypatch, tmp_path):
        # foil-serve reports failures as FastAPI HTTPExceptions: {"detail": ...} + 4xx/5xx.
        request = httpx.Request("POST", "http://foil-serve:8081/v1/process")
        response = httpx.Response(
            500, json={"detail": "PDF conversion error: boom"}, request=request
        )
        error = httpx.HTTPStatusError("500", request=request, response=response)

        saved = self._saved_error_json(monkeypatch, tmp_path, error)
        assert saved["upstream_details"] == {
            "status_code": 500,
            "url": "http://foil-serve:8081/v1/process",
            "body": {"detail": "PDF conversion error: boom"},
        }

    def test_unexpected_error_saved_with_traceback(self, monkeypatch, tmp_path):
        try:
            raise TypeError("expected string or bytes-like object")
        except TypeError as e:
            error = e
        saved = self._saved_error_json(monkeypatch, tmp_path, error)
        assert saved["upstream_details"] is None
        assert "TypeError: expected string" in saved["traceback"]


def _http_error(status: int, body) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://foil-serve:8081/v1/process")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


class TestClassifyUpstreamError:
    @pytest.mark.parametrize("code", [400, 413, 415, 422])
    def test_document_4xx_relayed_with_backend_detail(self, code):
        error = _http_error(code, {"detail": "File too large: 120 MB"})
        assert services.classify_upstream_error(error) == (
            code,
            "File too large: 120 MB",
        )

    def test_backend_5xx_becomes_502_with_backend_detail(self):
        error = _http_error(500, {"detail": "PDF conversion error: CUDA out of memory"})
        assert services.classify_upstream_error(error) == (
            502,
            "PDF conversion error: CUDA out of memory",
        )

    @pytest.mark.parametrize("code", [401, 403, 404])
    def test_gateway_misconfig_hides_detail(self, code):
        error = _http_error(code, {"detail": "Invalid API key"})
        assert services.classify_upstream_error(error) == (
            502,
            f"the backend answered HTTP {code}",
        )

    def test_non_json_body(self):
        request = httpx.Request("POST", "http://foil-serve:8081/v1/process")
        response = httpx.Response(500, text="<html>oops</html>", request=request)
        error = httpx.HTTPStatusError("500", request=request, response=response)
        assert services.classify_upstream_error(error) == (
            502,
            "the backend answered HTTP 500",
        )

    def test_urls_redacted(self):
        error = _http_error(
            502,
            {
                "detail": "External processor 'video' error: timeout for url 'http://video:9000/x'"
            },
        )
        _, reason = services.classify_upstream_error(error)
        assert "video:9000" not in reason and "<upstream>" in reason

    def test_processing_error_uses_its_status_and_reason(self):
        error = UpstreamProcessingError(
            "internal message", status_code=422, client_reason="no content"
        )
        assert services.classify_upstream_error(error) == (422, "no content")

    def test_timeout_unreachable_and_bug(self):
        request = httpx.Request("POST", "http://foil-serve:8081/v1/process")
        classify = services.classify_upstream_error
        assert classify(httpx.ReadTimeout("t", request=request))[0] == 504
        assert classify(TimeoutError())[0] == 504
        assert classify(httpx.ConnectError("refused", request=request)) == (
            502,
            "the backend is unreachable",
        )
        assert classify(TypeError("boom")) == (502, "internal error")


class TestUpstreamFailureDetail:
    VERSION = Enum("V", {"V1": "v1"}).V1
    DOCLING_ERROR = UpstreamProcessingError(
        "docling conversion failed: [VlmPipeline] HTML table exceeds 1000 grid cells",
        client_reason="[VlmPipeline] HTML table exceeds 1000 grid cells",
    )

    def _detail(self, monkeypatch, verbose: bool, error: Exception) -> tuple[int, str]:
        monkeypatch.setattr(services.settings, "verbose_errors", verbose)
        return services.upstream_failure_detail(error, self.VERSION, "f.pdf", "ab" * 32)

    def test_not_verbose_gives_backend_reason(self, monkeypatch):
        assert self._detail(monkeypatch, False, self.DOCLING_ERROR) == (
            502,
            "[v1] Processing failed for 'f.pdf': "
            "[VlmPipeline] HTML table exceeds 1000 grid cells (sha256: abababababab)",
        )

    def test_not_verbose_hides_url_and_raw_body(self, monkeypatch):
        status, detail = self._detail(
            monkeypatch, False, _http_error(500, {"detail": "boom", "trace": "secret"})
        )
        assert status == 502
        assert "boom" in detail
        assert "foil-serve" not in detail and "secret" not in detail

    def test_verbose_appends_raw_error_and_body(self, monkeypatch):
        _, detail = self._detail(
            monkeypatch, True, _http_error(500, {"detail": "boom", "trace": "secret"})
        )
        assert "Upstream error:" in detail and "secret" in detail


class TestEmptyFileRejected:
    def test_zero_bytes_is_422_without_backend_call(self, monkeypatch):
        async def must_not_run(**_kwargs):
            raise AssertionError("no backend call for an empty file")

        monkeypatch.setattr(services, "_resolve_document", must_not_run)
        headers = ExternalDocumentRequestHeaders.model_validate(
            {"Content-Type": "application/pdf", "X-Filename": "empty.pdf"}
        )
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                services.resolve_request(
                    headers_data=headers,
                    version=Enum("V", {"V1": "v1"}).V1,
                    config=make_foil_config(),
                    background_tasks=BackgroundTasks(),
                    api_key="key",
                    file_content=b"",
                    force_reprocess=False,
                )
            )
        assert exc_info.value.status_code == 422
        assert "empty" in exc_info.value.detail
