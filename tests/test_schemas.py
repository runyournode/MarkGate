"""Tests for ProcessedDocumentOut — the JSON-schema-safe public counterpart of ProcessedDocument."""

from PIL import Image

from schemas import Metadata, ProcessedDocument, ProcessedDocumentOut


def test_processed_document_out_has_a_json_schema():
    # Regression: ProcessedDocument itself can't be used as a FastAPI response_model — pydantic
    # can't generate a JSON Schema for its PIL.Image.Image-typed `images` field (arbitrary_types
    # bypasses validation, not schema generation). FastAPI would fail to build /openapi.json.
    # This must not raise.
    ProcessedDocumentOut.model_json_schema()


def test_processed_document_round_trips_into_processed_document_out():
    img = Image.new("RGB", (2, 2))
    doc = ProcessedDocument(
        page_content="hello", images={"a.png": img}, metadata=Metadata({"k": 1})
    )

    # This is the conversion main.py's /{version}/process route performs before returning.
    out = ProcessedDocumentOut(**doc.model_dump(mode="json"))

    assert out.page_content == "hello"
    assert isinstance(out.images["a.png"], str)  # base64, not a PIL.Image
    assert out.metadata.root == {"k": 1}
