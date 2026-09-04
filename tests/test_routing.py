"""Tests for the optional auto backend selection feature: routing.py (contract + loader),
auto_selector_demo.py (the shipped example), and auto_routes.py (the /auto/* routes themselves).
"""

import asyncio
from enum import Enum

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import auto_routes
from backends.foil import (
    FoilConfig,
    FoilQueryParams,
    SpreadsheetMode,
    SpreadsheetOverrides,
)
from config.settings import settings
from routing import BackendSelection, BackendSelectionContext, load_selector

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

FakeVersion = Enum("FakeVersion", {"foil": "foil"}, type=str)


def make_ctx(**overrides) -> BackendSelectionContext:
    defaults = dict(
        file_content=b"hello",
        filename="test.pdf",
        extension=".pdf",
        declared_content_type="application/pdf",
        sniffed_mime="application/pdf",
        size_bytes=5,
        available_versions={},
    )
    defaults.update(overrides)
    return BackendSelectionContext(**defaults)


class TestLoadSelector:
    def test_valid_file_loads_select_function(self, tmp_path, monkeypatch):
        selector_file = tmp_path / "sel.py"
        selector_file.write_text("async def select(ctx):\n    return None\n")
        monkeypatch.setattr(settings, "auto_selector_path", str(selector_file))
        assert callable(load_selector())

    def test_missing_path_setting_raises(self, monkeypatch):
        monkeypatch.setattr(settings, "auto_selector_path", None)
        with pytest.raises(ValueError, match="AUTO_SELECTOR_PATH"):
            load_selector()

    def test_missing_file_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "auto_selector_path", str(tmp_path / "nope.py"))
        with pytest.raises(FileNotFoundError):
            load_selector()

    def test_file_without_select_raises(self, tmp_path, monkeypatch):
        selector_file = tmp_path / "sel.py"
        selector_file.write_text("x = 1\n")
        monkeypatch.setattr(settings, "auto_selector_path", str(selector_file))
        with pytest.raises(TypeError, match="select"):
            load_selector()

    def test_non_callable_select_raises(self, tmp_path, monkeypatch):
        selector_file = tmp_path / "sel.py"
        selector_file.write_text("select = 42\n")
        monkeypatch.setattr(settings, "auto_selector_path", str(selector_file))
        with pytest.raises(TypeError, match="select"):
            load_selector()


class TestBackendSelectionContextAndSelection:
    def test_backend_selection_defaults_overrides_to_none(self):
        assert BackendSelection(version=FakeVersion.foil).overrides is None

    def test_context_is_frozen(self):
        ctx = make_ctx()
        with pytest.raises(Exception):
            ctx.size_bytes = 999  # type: ignore[misc]

    def test_selection_is_frozen(self):
        selection = BackendSelection(version=FakeVersion.foil)
        with pytest.raises(Exception):
            selection.version = FakeVersion.foil  # type: ignore[misc]


class TestAutoSelectorDemo:
    """Exercises the shipped example against the real Version enum (see tests/conftest.py /
    tests/fixtures/backend_config.toml, which declares the two names it references)."""

    def test_small_file_routes_to_ministral(self):
        import auto_selector_demo
        from config.loader import VERSION_CONFIGS, Version

        ctx = make_ctx(size_bytes=1024, available_versions=VERSION_CONFIGS)
        selection = asyncio.run(auto_selector_demo.select(ctx))
        assert selection.version == Version("foil-ministral-3-3b")
        assert selection.overrides is None

    def test_large_file_routes_to_foil_with_spreadsheet_overrides(self):
        import auto_selector_demo
        from config.loader import VERSION_CONFIGS, Version

        ctx = make_ctx(
            size_bytes=auto_selector_demo.SIZE_THRESHOLD_BYTES + 1,
            available_versions=VERSION_CONFIGS,
        )
        selection = asyncio.run(auto_selector_demo.select(ctx))
        assert selection.version == Version("foil")
        assert selection.overrides == SpreadsheetOverrides(
            spreadsheet_mode=SpreadsheetMode.AUTO, excel_min_output_ratio=0.99
        )

    def test_boundary_at_exactly_threshold_routes_to_foil(self):
        # size_bytes < THRESHOLD -> ministral; == THRESHOLD falls to the >= branch.
        import auto_selector_demo
        from config.loader import VERSION_CONFIGS, Version

        ctx = make_ctx(
            size_bytes=auto_selector_demo.SIZE_THRESHOLD_BYTES,
            available_versions=VERSION_CONFIGS,
        )
        selection = asyncio.run(auto_selector_demo.select(ctx))
        assert selection.version == Version("foil")


class TestAutoOverridesCacheConvergence:
    """A selector resolving to (foil, SpreadsheetOverrides(...)) must share its cache key with an
    explicit equivalent query-param request — same guarantee TestCacheKeyConvergence in
    test_foil_backend.py proves generally, pinned down here for the exact overrides the demo
    selector produces (see routing.py's module docstring / README's cache/lock coherence note)."""

    def test_demo_overrides_converge_with_explicit_query_params(self):
        base = FoilConfig(
            backend_type="foil",
            description="test",
            upstream_url="http://foil-serve:8081/v1/process",
            authorized_api_key="key",
        )
        via_auto = base.with_overrides(
            SpreadsheetOverrides(
                spreadsheet_mode=SpreadsheetMode.AUTO, excel_min_output_ratio=0.99
            )
        )
        via_explicit_query_params = FoilConfig(
            backend_type="foil",
            description="test",
            upstream_url="http://foil-serve:8081/v1/process",
            authorized_api_key="key",
            query_params=FoilQueryParams(
                spreadsheet_mode=SpreadsheetMode.AUTO, excel_min_output_ratio=0.99
            ),
        )
        assert via_auto.cache_key(
            "foil", XLSX_MIME
        ) == via_explicit_query_params.cache_key("foil", XLSX_MIME)


class TestAutoRoutesDisabled:
    def test_build_router_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setattr(settings, "auto_route_enabled", False)
        assert auto_routes.build_router() is None


class TestAutoRoutesEnabled:
    """Only exercises what happens before responders.run() (auth, size cap, selection) — same
    scope as the rest of this test suite, which never mocks the S3/Redis/upstream pipeline."""

    @pytest.fixture
    def enabled_router(self, tmp_path, monkeypatch):
        selector_file = tmp_path / "sel.py"
        selector_file.write_text(
            "from routing import BackendSelection\n"
            "from config.loader import Version\n"
            "async def select(ctx):\n"
            "    return BackendSelection(version=Version('foil'))\n"
        )
        monkeypatch.setattr(settings, "auto_route_enabled", True)
        monkeypatch.setattr(settings, "auto_selector_path", str(selector_file))
        monkeypatch.setattr(settings, "client_api_key_auto", "secret")
        monkeypatch.setattr(settings, "max_upload_size_bytes", None)
        return auto_routes.build_router()

    def _upload(self, client, **kwargs):
        return client.put(
            "/md/auto/process",
            content=kwargs.pop("content", b"hello"),
            headers={
                "Content-Type": "text/plain",
                "X-Filename": "a.txt",
                **kwargs.pop("headers", {}),
            },
            **kwargs,
        )

    def test_missing_api_key_is_rejected_before_selection_runs(self, enabled_router):
        client = TestClient(FastAPI())
        client.app.include_router(enabled_router)
        resp = self._upload(client)
        assert resp.status_code == 403

    def test_wrong_api_key_is_rejected(self, enabled_router):
        client = TestClient(FastAPI())
        client.app.include_router(enabled_router)
        resp = self._upload(client, headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 403

    def test_oversized_upload_is_rejected_before_select_is_called(
        self, enabled_router, tmp_path, monkeypatch
    ):
        calls = []
        selector_file = tmp_path / "sel_spy.py"
        selector_file.write_text(
            "from routing import BackendSelection\n"
            "from config.loader import Version\n"
            "async def select(ctx):\n"
            "    raise AssertionError('select() should not be called for oversized uploads')\n"
        )
        monkeypatch.setattr(settings, "max_upload_size_bytes", 3)
        monkeypatch.setattr(settings, "auto_selector_path", str(selector_file))
        router = auto_routes.build_router()  # reload with the spy + new size cap

        client = TestClient(FastAPI())
        client.app.include_router(router)
        resp = self._upload(
            client, content=b"way too big", headers={"Authorization": "Bearer secret"}
        )
        assert resp.status_code == 413
        assert calls == []

    def test_not_shadowed_by_the_explicit_version_route(self, enabled_router):
        # api.router's PUT /md/{version}/process would path-match "/md/auto/process" too and,
        # since "auto" isn't a real Version, fail FastAPI's own enum validation with a 422 shaped
        # like {"detail": [{"type": "enum", ...}]} — the regression this guards against.
        # auto_routes must therefore be included first; if it's shadowed, the missing-API-key
        # check below (403, from verify_api_key_auto) would instead come back as that 422.
        import api

        app = FastAPI()
        app.include_router(enabled_router)
        app.include_router(api.router)
        client = TestClient(app)
        resp = self._upload(client)
        assert resp.status_code == 403
