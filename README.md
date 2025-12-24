Proxy to convert documents into text:

- receive request from open-webui external document loader
- can manage different versions for conversion (configurable as the url/route in open-webui) to a single upstream
  converter
- cache the source file (based on hash) and conversions in S3 bucket:
    - cache hit → simply retrieve & serve the converted file
    - cache miss → request conversion from the upstream converter, cache and serve
    - sidecar file cache metadata : aliases, last access, number of access
- use redis locks to avoid race condition

# todo

- cache from multiple buckets:
    - if fails because file is new, add to a default bucket
    - if file is known but conversion version is uncached, add conversion in the source bucket
- upstreams url configurable in config section (enable multiple servers, e.g. one for each conversion version)
- fallback option to a no cache processing if S3 buckets are unreachable

## Resulting tree on S3 bucket

📂 S3 Bucket
└── 📂 documents/
└── 📂 abc12345.../  <-- Dossier racine du fichier (Hash)
├── 📄 source.pdf             <-- Fichier natif (téléchargeable/ouvrable)
├── 📄 _aliases.json          <-- Liste des noms de fichiers connus (Global)
├── 📂 v1/
│ ├── 📄 result.json       <-- Le Markdown généré
│ └── 📄 metadata.json     <-- Stats d'usage de la V1
└── 📂 v2/
├── 📄 result.json
└── 📄 metadata.json     <-- Stats d'usage de la V2

