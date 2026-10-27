"""Image storage: S3 layout per backend capability, write order, write-once shared images,
_images_names.txt round-trip, and the response metadata a client needs for POST /images."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from botocore.exceptions import ClientError
from fastapi import BackgroundTasks

import services
import storage
from backends.foil import FoilConfig
from backends.xberg import XbergConfig
from schemas import ExternalDocumentRequestHeaders, Metadata, ProcessedDocument

SHA = "0123456789abcdef" * 4
IMG_A = "imgs/aaaaaaaaaaaaaaaa.jpg"
IMG_B = "imgs/bbbbbbbbbbbbbbbb.jpg"


def make_foil_config() -> FoilConfig:
    return FoilConfig(
        backend_type="foil",
        description="test",
        upstream_url="http://foil-serve:8081/v1/process",
        authorized_api_key="key",
    )


def make_xberg_config() -> XbergConfig:
    return XbergConfig(
        backend_type="xberg",
        description="test",
        upstream_url="http://xberg-serve:8083/extract",
        authorized_api_key="key",
    )


class _Body:
    def __init__(self, data: bytes):
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeS3Client:
    """In-memory stand-in for the aiobotocore S3 client, recording write order."""

    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []

    @staticmethod
    def _missing(code: str) -> ClientError:
        return ClientError({"Error": {"Code": code}}, "op")

    async def put_object(self, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body.encode() if isinstance(Body, str) else Body
        self.puts.append(Key)

    async def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self._missing("404")

    async def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise self._missing("NoSuchKey")

        @asynccontextmanager
        async def body():
            yield _Body(self.objects[Key])

        return {"Body": body()}


@pytest.fixture
def s3(monkeypatch) -> FakeS3Client:
    client = FakeS3Client()
    monkeypatch.setattr(storage.s3_manager, "_client", client)
    return client


class TestBuildS3Keys:
    def test_content_named_images_are_shared_per_backend(self):
        _, content, _, image_keys = services.build_s3_keys(
            SHA, 'foil/image_description_model_name="m"', make_foil_config()
        )
        assert (
            content
            == f'documents/{SHA}/foil/image_description_model_name="m"/content.md'
        )
        assert image_keys.root == f"documents/{SHA}/foil"
        assert (
            image_keys.names_key
            == f'documents/{SHA}/foil/image_description_model_name="m"/_images_names.txt'
        )

    def test_other_backends_keep_images_per_signature(self):
        _, _, _, image_keys = services.build_s3_keys(SHA, "xberg", make_xberg_config())
        assert image_keys.root == f"documents/{SHA}/xberg/images"
        assert image_keys.names_key == f"documents/{SHA}/xberg/_images_names.txt"


class _Version:
    value = "test"


def _store(config, signature: str, images: dict[str, bytes]):
    _, content_key, metadata_key, image_keys = services.build_s3_keys(
        SHA, signature, config
    )
    document = ProcessedDocument(
        page_content="md", images=images, metadata=Metadata({"k": 1})
    )
    asyncio.run(
        services.update_s3_processed(
            document, config, content_key, metadata_key, image_keys, _Version(), "f.pdf"
        )
    )
    return image_keys


class TestUpdateS3Processed:
    def test_content_md_is_written_last(self, s3):
        _store(make_foil_config(), "foil", {IMG_A: b"a"})
        assert s3.puts[-1] == f"documents/{SHA}/foil/content.md"
        assert s3.puts.index(f"documents/{SHA}/foil/{IMG_A}") < s3.puts.index(
            f"documents/{SHA}/foil/_images_names.txt"
        )

    def test_shared_images_are_written_once(self, s3):
        foil = make_foil_config()
        _store(foil, "foil", {IMG_A: b"first"})
        _store(
            foil,
            'foil/image_description_model_name="m"',
            {IMG_A: b"second", IMG_B: b"b"},
        )

        assert s3.objects[f"documents/{SHA}/foil/{IMG_A}"] == b"first"
        assert s3.puts.count(f"documents/{SHA}/foil/{IMG_A}") == 1
        assert s3.objects[f"documents/{SHA}/foil/{IMG_B}"] == b"b"

    def test_per_signature_images_are_overwritten(self, s3):
        xberg = make_xberg_config()
        _store(xberg, "xberg", {IMG_A: b"first"})
        _store(xberg, "xberg", {IMG_A: b"second"})
        assert s3.objects[f"documents/{SHA}/xberg/images/{IMG_A}"] == b"second"

    def test_images_names_written_even_without_images(self, s3):
        _store(make_foil_config(), "foil", {})
        assert s3.objects[f"documents/{SHA}/foil/_images_names.txt"] == b""


class TestGatherImagesFromCache:
    def _gather(self, image_keys):
        return asyncio.run(
            services.gather_images(
                ProcessedDocument(page_content="md"),
                True,
                image_keys,
                _Version(),
                "f.pdf",
            )
        )

    def test_serves_only_the_images_listed_for_this_processing(self, s3):
        foil = make_foil_config()
        _store(
            foil, 'foil/image_description_model_name="m"', {IMG_A: b"a", IMG_B: b"b"}
        )
        plain_keys = _store(foil, "foil", {IMG_A: b"a"})

        assert self._gather(plain_keys) == {IMG_A: b"a"}

    def test_missing_names_file_serves_no_image(self, s3):
        _, _, _, image_keys = services.build_s3_keys(SHA, "foil", make_foil_config())
        assert self._gather(image_keys) == {}

    def test_listed_image_missing_from_s3_is_skipped(self, s3):
        image_keys = _store(make_foil_config(), "foil", {IMG_A: b"a", IMG_B: b"b"})
        del s3.objects[f"{image_keys.root}/{IMG_B}"]
        assert self._gather(image_keys) == {IMG_A: b"a"}

    def test_fresh_processing_returns_backend_images_without_io(self):
        document = ProcessedDocument(page_content="md", images={IMG_A: b"a"})
        _, _, _, image_keys = services.build_s3_keys(SHA, "foil", make_foil_config())
        images = asyncio.run(
            services.gather_images(document, False, image_keys, _Version(), "f.pdf")
        )
        assert images == {IMG_A: b"a"}


class TestGetImagesByName:
    def test_found_and_missing(self, s3):
        _store(make_foil_config(), "foil", {IMG_A: b"a"})
        images, missing = asyncio.run(
            services.get_images_by_name(SHA, make_foil_config(), [IMG_A, IMG_B])
        )
        assert images == {IMG_A: b"a"}
        assert missing == [IMG_B]


class TestResolveRequestMetadata:
    def test_response_metadata_carries_source_hash_and_engine(self, monkeypatch):
        async def fake_resolve(**kwargs):
            # e.g. a cache entry written before extraction_engine existed
            return ProcessedDocument(
                page_content="md", metadata=Metadata({"k": 1})
            ), True

        monkeypatch.setattr(services, "_resolve_document", fake_resolve)
        monkeypatch.setattr(services, "s3_is_active", lambda: False)
        content = b"%PDF-1.4 test"

        document, *_ = asyncio.run(
            services.resolve_request(
                ExternalDocumentRequestHeaders(
                    **{"Content-Type": "application/pdf", "X-Filename": "f.pdf"}
                ),
                _Version(),
                make_foil_config(),
                BackgroundTasks(),
                "key-1234",
                content,
                False,
            )
        )
        assert document.metadata.root == {
            "k": 1,
            "source_sha256": services.compute_hash(content),
            "extraction_engine": "foil",
        }
