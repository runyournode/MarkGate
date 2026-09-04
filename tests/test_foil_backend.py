"""Tests for backends/foil.py's per-request overrides and cache-key convergence, and for the
inert base-class defaults (contracts.ProcessingConfig) that every other backend still relies on.
"""

from backends.docling import DoclingConfig
from backends.foil import (
    FoilConfig,
    FoilQueryParams,
    SpreadsheetMode,
    SpreadsheetOverrides,
)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME = "application/pdf"


def make_foil_config(**query_params_kwargs) -> FoilConfig:
    return FoilConfig(
        backend_type="foil",
        description="test",
        upstream_url="http://foil-serve:8081/v1/process",
        authorized_api_key="key",
        query_params=FoilQueryParams(**query_params_kwargs),
    )


def make_docling_config(**kwargs) -> DoclingConfig:
    return DoclingConfig(
        backend_type="docling",
        description="test",
        upstream_url="http://docling-serve:5001",
        authorized_api_key="key",
        **kwargs,
    )


class TestGetRawQueryParams:
    def test_spreadsheet_params_dropped_for_non_spreadsheet_mime(self):
        config = make_foil_config(
            spreadsheet_mode=SpreadsheetMode.OCR, excel_min_output_ratio=0.5
        )
        params = config.get_raw_query_params(PDF_MIME)
        assert "spreadsheet_mode" not in params
        assert "excel_min_output_ratio" not in params

    def test_spreadsheet_mode_kept_for_spreadsheet_mime(self):
        config = make_foil_config(spreadsheet_mode=SpreadsheetMode.OCR)
        params = config.get_raw_query_params(XLSX_MIME)
        assert params["spreadsheet_mode"] == SpreadsheetMode.OCR

    def test_excel_min_output_ratio_dropped_outside_auto_mode(self):
        config = make_foil_config(
            spreadsheet_mode=SpreadsheetMode.OCR, excel_min_output_ratio=0.2
        )
        params = config.get_raw_query_params(XLSX_MIME)
        assert "excel_min_output_ratio" not in params

    def test_excel_min_output_ratio_kept_in_auto_mode(self):
        config = make_foil_config(
            spreadsheet_mode=SpreadsheetMode.AUTO, excel_min_output_ratio=0.2
        )
        params = config.get_raw_query_params(XLSX_MIME)
        assert params["excel_min_output_ratio"] == 0.2

    def test_empty_vlm_name_is_dropped_without_wiping_other_params(self):
        # Regression: the previous implementation dropped the *entire* params dict as soon as
        # image_description_model_name was empty — it must now only drop that one key.
        config = make_foil_config(spreadsheet_mode=SpreadsheetMode.OCR)
        params = config.get_raw_query_params(XLSX_MIME)
        assert "image_description_model_name" not in params
        assert params["spreadsheet_mode"] == SpreadsheetMode.OCR

    def test_vlm_name_is_kept_when_set(self):
        config = make_foil_config(image_description_model_name="ministral-3-3b")
        params = config.get_raw_query_params(PDF_MIME)
        assert params["image_description_model_name"] == "ministral-3-3b"


class TestWithOverrides:
    def test_no_overrides_returns_same_instance(self):
        config = make_foil_config()
        assert config.with_overrides(None) is config

    def test_override_merges_over_static_config(self):
        config = make_foil_config(image_description_model_name="ministral-3-3b")
        overridden = config.with_overrides(
            SpreadsheetOverrides(spreadsheet_mode=SpreadsheetMode.OCR)
        )
        assert overridden.query_params.spreadsheet_mode == SpreadsheetMode.OCR
        assert overridden.query_params.image_description_model_name == "ministral-3-3b"
        # The original config is untouched.
        assert config.query_params.spreadsheet_mode == SpreadsheetMode.AUTO

    def test_unset_override_fields_do_not_clobber_the_static_config(self):
        config = make_foil_config(spreadsheet_mode=SpreadsheetMode.OCR)
        overridden = config.with_overrides(
            SpreadsheetOverrides(excel_min_output_ratio=0.05)
        )
        assert overridden.query_params.spreadsheet_mode == SpreadsheetMode.OCR
        assert overridden.query_params.excel_min_output_ratio == 0.05


class TestCacheKeyConvergence:
    """The whole point: a preset Version and an equivalent per-request override must resolve to
    the same cache/lock key, without either being declared as an alias of the other anywhere.
    """

    def test_preset_version_and_override_converge(self):
        preset = make_foil_config(spreadsheet_mode=SpreadsheetMode.OCR)
        base = make_foil_config().with_overrides(
            SpreadsheetOverrides(spreadsheet_mode=SpreadsheetMode.OCR)
        )
        assert preset.cache_key("foil-spreadsheet-ocr", XLSX_MIME) == base.cache_key(
            "foil", XLSX_MIME
        )

    def test_omitted_and_explicit_auto_converge(self):
        omitted = make_foil_config()
        explicit = make_foil_config().with_overrides(
            SpreadsheetOverrides(spreadsheet_mode=SpreadsheetMode.AUTO)
        )
        assert omitted.cache_key("foil", XLSX_MIME) == explicit.cache_key(
            "foil", XLSX_MIME
        )

    def test_non_spreadsheet_mime_neutralizes_spreadsheet_mode_difference(self):
        ocr_mode = make_foil_config(spreadsheet_mode=SpreadsheetMode.OCR)
        auto_mode = make_foil_config(spreadsheet_mode=SpreadsheetMode.AUTO)
        assert ocr_mode.cache_key(
            "foil-spreadsheet-ocr", PDF_MIME
        ) == auto_mode.cache_key("foil", PDF_MIME)

    def test_different_effective_params_diverge(self):
        ocr = make_foil_config(spreadsheet_mode=SpreadsheetMode.OCR)
        both = make_foil_config(spreadsheet_mode=SpreadsheetMode.BOTH)
        assert ocr.cache_key("foil-ocr", XLSX_MIME) != both.cache_key(
            "foil-both", XLSX_MIME
        )

    def test_different_vlm_model_diverges_even_with_same_spreadsheet_mode(self):
        a = make_foil_config(image_description_model_name="ministral-3-3b")
        b = make_foil_config(image_description_model_name="qwen-3.5-27b")
        assert a.cache_key("foil-ministral", PDF_MIME) != b.cache_key(
            "foil-qwen", PDF_MIME
        )


class TestProcessingConfigBaseDefaults:
    """Non-regression: a backend that doesn't override with_overrides()/cache_key() (e.g.
    docling) keeps today's exact behavior — keyed on cache_id/version, overrides ignored.
    """

    def test_cache_key_uses_cache_id_when_set(self):
        config = make_docling_config(cache_id="docling-v1")
        assert config.cache_key("docling", PDF_MIME) == "docling-v1"

    def test_cache_key_falls_back_to_version_without_cache_id(self):
        config = make_docling_config()
        assert config.cache_key("docling", PDF_MIME) == "docling"

    def test_with_overrides_is_a_no_op(self):
        config = make_docling_config()
        assert (
            config.with_overrides(
                SpreadsheetOverrides(spreadsheet_mode=SpreadsheetMode.OCR)
            )
            is config
        )
