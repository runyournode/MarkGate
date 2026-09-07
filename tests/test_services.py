"""Tests for services.call_upstream_backend's extraction_engine stamping — the single place
every backend's result passes through, so this is the only place that needs covering (not each
backends/*.py module)."""

import asyncio

import services
from backends.foil import FoilConfig, FoilQueryParams
from schemas import Metadata, ProcessedDocument


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

    async def _fake_handler_no_metadata(self, config, file_content, headers, filename, client):
        return ProcessedDocument(page_content="hello")

    def test_stamps_backend_type_without_clobbering_existing_metadata(self, monkeypatch):
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
