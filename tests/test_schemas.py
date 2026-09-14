"""Tests for ProcessedDocument / ProcessedDocumentOut and the POST /images request model."""

import base64

import pytest
from pydantic import ValidationError

from schemas import ImagesRequest, Metadata, ProcessedDocument, ProcessedDocumentOut

SHA = "a" * 64


def test_processed_document_out_has_a_json_schema():
    # FastAPI builds /openapi.json from it: must not raise.
    ProcessedDocumentOut.model_json_schema()


def test_processed_document_round_trips_into_processed_document_out():
    raw = b"\xff\xd8\xff\xe0 raw jpeg bytes"
    doc = ProcessedDocument(
        page_content="hello",
        images={"imgs/6f715daebc26828f.jpg": raw},
        metadata=Metadata({"k": 1}),
    )

    # This is the conversion the /{version}/process route performs before returning.
    out = ProcessedDocumentOut(**doc.model_dump(mode="json"))

    assert out.page_content == "hello"
    # base64 of the exact bytes, never re-encoded
    assert base64.b64decode(out.images["imgs/6f715daebc26828f.jpg"]) == raw
    assert out.metadata.root == {"k": 1}


class TestImagesRequest:
    def test_valid(self):
        request = ImagesRequest(engine="foil", source_sha256=SHA, names=["x.jpg"])
        assert request.names == ["x.jpg"]

    @pytest.mark.parametrize("names", [[], [f"{i}.jpg" for i in range(51)]])
    def test_names_count_is_bounded(self, names):
        with pytest.raises(ValidationError):
            ImagesRequest(engine="foil", source_sha256=SHA, names=names)

    def test_names_is_required(self):
        with pytest.raises(ValidationError):
            ImagesRequest.model_validate({"engine": "foil", "source_sha256": SHA})

    @pytest.mark.parametrize("sha", ["A" * 64, "a" * 63, "g" * 64, ""])
    def test_source_sha256_must_be_lowercase_hex_64(self, sha):
        with pytest.raises(ValidationError):
            ImagesRequest(engine="foil", source_sha256=sha, names=["x.jpg"])

    def test_unknown_field_is_rejected(self):
        with pytest.raises(ValidationError):
            ImagesRequest.model_validate(
                {
                    "engine": "foil",
                    "source_sha256": SHA,
                    "names": ["x.jpg"],
                    "version": "foil",
                }
            )
