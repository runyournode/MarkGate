MarkGate
====

<img height="200" src="src/markgate/statics/markgate_banner.jpg" title="MarkGate Banner"/>  

**MarkGate**, a proxy for Markdown converter backends with persistent and versioned cache.

# Features

Receive request from open-webui external document loader.

Can manage different versions for conversion (configurable as the url/route in open-webui) to multiple upstream
converters.

Cache the source file (based on hash) and conversions in S3 bucket:

- cache hit → simply retrieve & serve the converted file
- cache miss → request conversion from the upstream converter, cache and serve

Sidecar file cache metadata : aliases, last access, number of access.

Use redis locks to avoid race condition.

# Known Issues:

 - [minor] Requesting processing a file in cache but with a different filename will uselessly reupload the file to S3 (upstream processing won't be called ).  
  As a result we could result in `source.bin` `source.pdf` stored on the s3. See Todo: resolve mime-type

# Dev status
 - Well tested for v1.0.0 and v1.1.0 config only (docling (v4) was quicly tested).
 - docker/ define some docling and paddle server, do not use them in production



# ToDo
- :warning: **Tuning all the time-out**
- auto resolve mime time (to avoid saving in S3 a source.bin if can e.g save a source.pdf or source.xlsx )
- cache from multiple buckets:
    - if Miss because file is new, add to a default bucket
    - if file is known but conversion version is uncached, add conversion in the source bucket
- fallback option to a no cache processing if S3 buckets are unreachable

## S3 Bucket Structure

```text
📂 S3 Bucket
└── 📂 documents/
    └── 📂 abc12345.../            # Content-based hash (Root)
        ├── 📄 source.pdf          # Native file
        ├── 📄 _aliases.json       # Known original filenames
        ├── 📂 v1/
        │   ├── 📄 content.md     # Generated Markdown
        │   ├── 📄 metadata.json  # Generated Metadata
        │   └── 📄 _metadata.json   # Cache metadata (hits, last access, last filename used) 
        └── 📂 v2/
            ├── 📄 content.md
            ├── 📄 metadata.json
            └── 📄 _metadata.json
```