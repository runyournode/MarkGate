# AGENTS.md

## Purpose

This project implements a **FastAPI-based proxy** between an **open-webui (owui) client** and **backend processing
services**.

The backend processors are responsible for converting files into **Markdown** using advanced techniques (out of scope
for this repository).

The proxy acts as:

- a **request router**
- a **cache manager**
- a **logging layer**
- a **concurrency guard**

---

## Architecture Overview

### Components

- **FastAPI application**
    - Exposes HTTP APIs consumed by open-webui
    - Forwards requests to the upstream backend processor

- **Backend processor (upstream)**
    - Performs file-to-Markdown conversion
    - Treated as an external dependency

- **S3-compatible storage**
    - Used for persistent caching of processed results

- **Redis**
    - Used exclusively as a **distributed lock**
    - Prevents race conditions on identical processing requests
    - Not used as a cache or datastore

---

## Responsibilities of This Service

This service **MUST**:

- Route incoming requests to the appropriate backend endpoint
- Avoid duplicate processing using Redis locks
- Store and retrieve cached results from S3
- Log requests, responses, and processing decisions clearly
- Fail fast and explicitly when upstream services are unavailable

This service **MUST NOT**:

- Perform file processing or content transformation
- Contain backend processor logic
- Rely on Redis for data persistence

---

## Technical Guidelines

- Use the **latest stable version of FastAPI**
- Leverage **Pydantic v2** models for:
    - request validation
    - response schemas
    - internal data contracts
- Prefer:
    - dependency injection
    - background tasks when appropriate
    - explicit typing everywhere
- Keep implementation details at the **right abstraction level**
    - avoid over-engineering
    - avoid hidden magic

---

## Caching & Concurrency Strategy

- Cache keys must be:
    - deterministic
    - derived from request inputs
- Redis locks must:
    - be short-lived
    - have explicit timeouts
    - be released safely on failures
- S3 is the **single source of truth** for cached results

---

## Project Constraints & Tooling

- Dependency management uses:
    - `uv`
    - `pyproject.toml`
- The application **must be containerizable**
    - A Docker image is produced as part of the build
- Configuration must be:
    - environment-driven
    - compatible with container deployment

---

## Coding Style Expectations

- Code should be:
    - readable
    - explicit
    - well-documented
- Non-obvious decisions **must be explained**
    - either in code comments
    - or directly in the response
- Logging must be:
    - structured
    - meaningful
    - production-ready

---

## External Documentation

Open-webui client send request to this proxy by calling the `load` method from `ExternalDocumentLoader` :  
[open-webui/backend/open_webui/retrieval/loaders/external_document.py](https://github.com/open-webui/open-webui/blob/6f1486ffd0cb288d0e21f41845361924e0d742b3/backend/open_webui/retrieval/loaders/external_document.py#L7)  
**Important**: We assume in this project that `headers["Content-Type"]` and  `headers["X-Filename"]` **are never None**.

```python
import requests
import logging, os
from typing import Iterator, List, Union
from urllib.parse import quote

from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document
from open_webui.utils.headers import include_user_info_headers


class ExternalDocumentLoader(BaseLoader):
    def __init__(
            self,
            file_path,
            url: str,
            api_key: str,
            mime_type=None,
            user=None,
            **kwargs,
    ) -> None:
        self.url = url
        self.api_key = api_key

        self.file_path = file_path
        self.mime_type = mime_type

        self.user = user

    def load(self) -> List[Document]:
        with open(self.file_path, "rb") as f:
            data = f.read()

        headers = {}
        if self.mime_type is not None:
            headers["Content-Type"] = self.mime_type

        if self.api_key is not None:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            headers["X-Filename"] = quote(os.path.basename(self.file_path))
        except:
            pass

        if self.user is not None:
            headers = include_user_info_headers(headers, self.user)

        url = self.url
        if url.endswith("/"):
            url = url[:-1]

        try:
            response = requests.put(f"{url}/process", data=data, headers=headers)
        except Exception as e:
            raise Exception(f"Error connecting to endpoint: {e}")

        if response.ok:

            response_data = response.json()
            if response_data:
                if isinstance(response_data, dict):
                    return [
                        Document(
                            page_content=response_data.get("page_content"),
                            metadata=response_data.get("metadata"),
                        )
                    ]
                elif isinstance(response_data, list):
                    documents = []
                    for document in response_data:
                        documents.append(
                            Document(
                                page_content=document.get("page_content"),
                                metadata=document.get("metadata"),
                            )
                        )
                    return documents
                else:
                    raise Exception("Error loading document: Unable to parse content")

            else:
                raise Exception("Error loading document: No content returned")
        else:
            raise Exception(
                f"Error loading document: {response.status_code} {response.text}"
            )
```

---
