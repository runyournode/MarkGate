"""Tests for backends/xberg.py's config schema — construction, param resolution, and the
inert base-class with_overrides/cache_key defaults (xberg doesn't override either)."""

from backends.xberg import XbergConfig, XbergQueryParams, _simplify_metadata


def make_xberg_config(**query_params_kwargs) -> XbergConfig:
    return XbergConfig(
        backend_type="xberg",
        description="test",
        upstream_url="http://xberg-serve:8083/extract",
        authorized_api_key="key",
        query_params=XbergQueryParams(**query_params_kwargs),
    )


class TestXbergQueryParams:
    def test_output_format_defaults_to_markdown(self):
        config = make_xberg_config()
        assert config.get_raw_query_params()["output_format"] == "markdown"

    def test_output_format_overridable(self):
        config = make_xberg_config(output_format="plain")
        assert config.get_raw_query_params()["output_format"] == "plain"

    def test_extra_keys_are_allowed(self):
        # xberg's ExtractionConfig has many more keys than we model — passthrough must work.
        # Use a key that isn't one of the typed fields below, so this actually exercises
        # extra="allow" rather than a modeled field.
        config = XbergQueryParams(output_format="markdown", content_filter="boilerplate")
        assert config.model_dump()["content_filter"] == "boilerplate"

    def test_common_params_default_to_unset(self):
        # None (xberg's own default applies) → dropped by exclude_none, same as before typing.
        config = make_xberg_config()
        params = config.get_raw_query_params()
        for key in ("force_ocr", "ocr", "use_cache", "ocr_strategy", "extraction_timeout_secs"):
            assert key not in params

    def test_common_params_overridable(self):
        config = make_xberg_config(
            force_ocr=True,
            use_cache=False,
            ocr_strategy="tesseract",
            extraction_timeout_secs=120,
        )
        params = config.get_raw_query_params()
        assert params["force_ocr"] is True
        assert params["use_cache"] is False
        assert params["ocr_strategy"] == "tesseract"
        assert params["extraction_timeout_secs"] == 120


class TestSimplifyMetadata:
    # Real shape returned by a running xberg instance (see backends/xberg.py's
    # _simplify_metadata docstring for why it's flattened/filtered).
    RAW = {
        "created_at": "2011-11-18T11:11:24Z",
        "modified_at": "2011-11-21T10:42:12Z",
        "format": {
            "format_type": "pdf",
            "pdf_version": "1.5",
            "is_encrypted": False,
            "width": 438,
            "height": 691,
            "page_count": 56,
            "scanned_confidence": 0.85,
            "scanned_pages": [1, 2, 3],
        },
        "extraction_duration_ms": 1569,
        "output_format": "markdown",
        "ocr_used": False,
        "additional": {
            "source_kind": "bytes",
            "final_uri": "10840.pdf",
            "source_index": 0,
            "source_uri": "10840.pdf",
            "extraction_method": "native",
        },
    }

    def test_keeps_only_the_relevant_fields(self):
        assert _simplify_metadata(self.RAW) == {
            "created_at": "2011-11-18T11:11:24Z",
            "modified_at": "2011-11-21T10:42:12Z",
            "page_count": 56,
            "scanned_pages": [1, 2, 3],
            "extraction_duration_ms": 1569,
            "ocr_used": False,
            "extraction_method": "native",
        }

    def test_missing_source_fields_are_dropped_not_nulled(self):
        assert _simplify_metadata({}) == {}
        assert _simplify_metadata({"ocr_used": False}) == {"ocr_used": False}


class TestProcessingConfigBaseDefaults:
    """xberg doesn't override with_overrides()/cache_key() — same as docling."""

    def test_cache_key_falls_back_to_version_without_cache_id(self):
        config = make_xberg_config()
        assert config.cache_key("xberg", "application/pdf") == "xberg"

    def test_with_overrides_is_a_no_op(self):
        config = make_xberg_config()
        assert config.with_overrides(None) is config
