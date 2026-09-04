"""Tests for backend_config.toml route aliases (backends.foil.FoilRouteAlias) — parsing,
validation, and resolution (config.loader._resolve_aliases / ALIAS_ROUTES).
"""

from enum import Enum

import pytest
from pydantic import ValidationError

from backends.docling import DoclingConfig
from backends.foil import FoilConfig, FoilQueryParams, FoilRouteAlias, SpreadsheetMode
from config.loader import ResolvedAlias, _resolve_aliases
from contracts import ProcessingConfig

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def make_foil_config(
    aliases: list[FoilRouteAlias] | None = None, **query_params_kwargs
) -> FoilConfig:
    return FoilConfig(
        backend_type="foil",
        description="test",
        upstream_url="http://foil-serve:8081/v1/process",
        authorized_api_key="key",
        query_params=FoilQueryParams(**query_params_kwargs),
        aliases=aliases or [],
    )


def make_docling_config(**kwargs) -> DoclingConfig:
    return DoclingConfig(
        backend_type="docling",
        description="test",
        upstream_url="http://docling-serve:5001",
        authorized_api_key="key",
        **kwargs,
    )


FakeVersion = Enum(
    "FakeVersion",
    {"foil": "foil", "docling": "docling", "foil_alt": "foil-alt"},
    type=str,
)


class TestFoilRouteAliasValidation:
    def test_valid_name_and_query_params(self):
        alias = FoilRouteAlias(
            name="spreadsheet_ocr", query_params={"spreadsheet_mode": "ocr"}
        )
        assert alias.name == "spreadsheet_ocr"
        assert alias.query_params.spreadsheet_mode == SpreadsheetMode.OCR

    @pytest.mark.parametrize("bad_name", ["", "Spreadsheet-OCR", "has space", "a/b"])
    def test_invalid_name_is_rejected(self, bad_name):
        with pytest.raises(ValidationError):
            FoilRouteAlias(name=bad_name, query_params={})

    def test_unknown_query_param_is_rejected(self):
        with pytest.raises(ValidationError):
            FoilRouteAlias(name="bogus", query_params={"not_a_real_param": 1})

    def test_docling_config_rejects_aliases_field(self):
        # DoclingConfig has no `aliases` field — ProcessingConfig's extra="forbid" must reject
        # it at construction (mirrors what BackendConfig validation does for backend_config.toml).
        with pytest.raises(ValidationError):
            DoclingConfig(
                backend_type="docling",
                description="test",
                upstream_url="http://docling-serve:5001",
                authorized_api_key="key",
                aliases=[{"name": "x", "query_params": {}}],
            )


class TestResolveAliases:
    def test_empty_aliases_produce_no_routes(self):
        configs: dict[Enum, ProcessingConfig] = {FakeVersion.foil: make_foil_config()}
        assert _resolve_aliases(configs) == []

    def test_backend_without_aliases_field_is_skipped(self):
        configs: dict[Enum, ProcessingConfig] = {
            FakeVersion.docling: make_docling_config()
        }
        assert _resolve_aliases(configs) == []

    def test_alias_resolves_to_merged_config(self):
        base = make_foil_config(
            aliases=[
                FoilRouteAlias(
                    name="spreadsheet_ocr", query_params={"spreadsheet_mode": "ocr"}
                )
            ]
        )
        configs: dict[Enum, ProcessingConfig] = {FakeVersion.foil: base}
        [resolved] = _resolve_aliases(configs)
        assert isinstance(resolved, ResolvedAlias)
        assert resolved.version is FakeVersion.foil
        assert resolved.name == "spreadsheet_ocr"
        assert resolved.config.query_params.spreadsheet_mode == SpreadsheetMode.OCR
        # The parent config is untouched.
        assert base.query_params.spreadsheet_mode == SpreadsheetMode.AUTO

    def test_duplicate_alias_name_raises(self):
        base = make_foil_config(
            aliases=[
                FoilRouteAlias(name="dup", query_params={"spreadsheet_mode": "ocr"}),
                FoilRouteAlias(name="dup", query_params={"spreadsheet_mode": "both"}),
            ]
        )
        configs: dict[Enum, ProcessingConfig] = {FakeVersion.foil: base}
        with pytest.raises(ValueError, match="Duplicate route alias name"):
            _resolve_aliases(configs)

    def test_same_alias_name_on_different_backends_is_allowed(self):
        foil_a = make_foil_config(
            aliases=[
                FoilRouteAlias(name="ocr", query_params={"spreadsheet_mode": "ocr"})
            ]
        )
        foil_b = make_foil_config(
            aliases=[
                FoilRouteAlias(name="ocr", query_params={"spreadsheet_mode": "both"})
            ]
        )
        configs: dict[Enum, ProcessingConfig] = {
            FakeVersion.foil: foil_a,
            FakeVersion.foil_alt: foil_b,
        }
        resolved = _resolve_aliases(configs)
        assert len(resolved) == 2


class TestAliasCacheKeyConvergence:
    """An alias route must be indistinguishable, cache-wise, from the equivalent query-param
    request on its parent Version — same guarantee as TestCacheKeyConvergence in
    test_foil_backend.py, exercised here through the alias resolution path."""

    def test_resolved_alias_converges_with_equivalent_override(self):
        base = make_foil_config(
            aliases=[
                FoilRouteAlias(
                    name="spreadsheet_ocr", query_params={"spreadsheet_mode": "ocr"}
                )
            ]
        )
        [resolved] = _resolve_aliases({FakeVersion.foil: base})

        from backends.foil import SpreadsheetOverrides

        via_query_param = base.with_overrides(
            SpreadsheetOverrides(spreadsheet_mode=SpreadsheetMode.OCR)
        )

        assert resolved.config.cache_key(
            "foil", XLSX_MIME
        ) == via_query_param.cache_key("foil", XLSX_MIME)
