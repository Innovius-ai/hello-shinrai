# Hello ShinrAI

Hello ShinrAI is a local playground for learning, testing, and debugging ShinrAI. It gives you a browser workspace for text protection, document and scan handling, protected LLM chat, Azure Language comparisons, and provider-compatible API calls. The request trace shows what ShinrAI received, what the LLM received, and what was restored locally.

Hello ShinrAI uses the [ShinrAI native PII API v2](https://shinrai.innovius.io/public-docs/pii-api-v2.md) by default. The v1 routes stay available as a visible option in Text and Files.

The app binds only to `127.0.0.1`. API keys remain in the local Python process unless you explicitly save them in your operating system's credential store. There is no database, frontend build, or local model download.

## Run it

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/) once, then run the tagged release:

```bash
uvx --from https://github.com/Innovius-ai/hello-shinrai/releases/download/v0.1.4/hello_shinrai-0.1.4-py3-none-any.whl hello-shinrai
```

The browser opens at `http://127.0.0.1:8765`. Use another port or skip browser opening when needed:

```bash
uvx --from https://github.com/Innovius-ai/hello-shinrai/releases/download/v0.1.4/hello_shinrai-0.1.4-py3-none-any.whl hello-shinrai --port 8877 --no-browser
```

To upgrade, change both version values in the wheel URL. To run the current development branch, use `uvx --from git+https://github.com/Innovius-ai/hello-shinrai hello-shinrai`.

### Conventional Python install

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install .
hello-shinrai
```

On Windows PowerShell, activate with `.venv\Scripts\Activate.ps1`.

### Docker

```bash
docker build -t hello-shinrai .
docker run --rm -p 127.0.0.1:8765:8765 hello-shinrai
```

After a tagged release, the same image is available from GitHub Container Registry:

```bash
docker run --rm -p 127.0.0.1:8765:8765 ghcr.io/innovius-ai/hello-shinrai:v0.1.4
```

## First request

1. Open **Connections** and enter your ShinrAI API key. The managed base URL defaults to `https://api.shinrai.innovius.io` and can be changed for sandbox or on-premises deployments.
2. Connect. Hello ShinrAI reads `GET /v2/capabilities` and `GET /v2/usage`, then shows available models, balance, and Standard, Batch, and Real-time access. Real-time is labelled as the fast tier. A deployment without API v2 answers 404; the app then reads `/v1/models` and `/v1/usage` and switches Text and Files to v1.
3. Open **Text**, keep the supplied synthetic example, and run protection.
4. Expand the trace on the right to inspect the request, response, request IDs, timing, findings, mapping, and usage.

A ShinrAI key is enough for Text, Files, Azure-shaped calls to ShinrAI, AWS Comprehend compatibility, and Google DLP compatibility. Chat additionally needs an LLM provider key.

## What each workspace calls

| Workspace | Native API v2 (default) | v1 option |
| --- | --- | --- |
| Text · Detect only | `POST /v2/detect` | `POST /v1/analyze` |
| Text · Protect text, Consistent batch | `POST /v2/protect` with `policy.preset` and `output.include: ["entities", "mapping"]` | `POST /v1/redact/batch` |
| Text · Restore locally | No request: the last replacement map restores the text in this process | Same |
| Text · Restore via /v2/restore | `POST /v2/restore` with `mapping.known` (free, sends the originals of the map) | — |
| Files · TXT | `POST /v2/protect` | `/v1/documents/jobs` |
| Files · PNG, JPEG, BMP | `POST /v2/protect` with an image input: filled image, pixel boxes, mapping | Image-only PDF through `/v1/documents/jobs` |
| Files · PDF, DOCX | `POST /v2/uploads`, `POST /v2/jobs` (`kind: document`), artifacts `protected`, `entities`, `mapping`, then `DELETE /v2/jobs/{id}` | `/v1/documents/jobs` with a flattened redacted PDF |
| Chat | `POST /v2/protect` with earlier pairs in `mapping.known` | — |
| Connections | `GET /v2/capabilities`, `GET /v2/usage` | `GET /v1/models`, `GET /v1/usage` |

The modes map to v2 presets: **Pseudonymize** is `pseudonymize`, **Mask** is `mask`, and **Label** is `label`. Pseudonymize and Label are reversible, so Text and Chat restore them locally. The model field accepts `latest` or a version such as `v1.4`. Without a custom confidence floor, the model's served floor applies.

## Protected chat

Open **Connections**, choose Innovius, OpenAI, Kie, or Custom, and configure the model-list URL, Chat Completions or Responses URL, corresponding key, and model ID.

Model discovery accepts common OpenAI-style `data` and `models` catalogues. A model appearing in a catalogue does not prove that it accepts Responses or image input. Kie uses model-specific Chat Completions URLs for some models; copy the endpoint shown in that model's Kie documentation. Kie's Responses-compatible Codex route uses `https://api.kie.ai/api/v1/responses`.

Every chat turn protects the complete local conversation with one `POST /v2/protect` before contacting the model. API v2 draws new surrogates for every request, so the app sends the pairs of earlier turns as `mapping.known`: a person keeps one stand-in for the whole conversation. Protection failure stops the request. The model sees replacements and the answer is restored locally, including replacements split across streamed chunks. The app asks supported providers not to store model responses and does not use hosted conversation state or execute tools.

Files can be sent as protected text. For vision-capable models, you can also send images that come exclusively from ShinrAI's output: the filled image of API v2, or the pages of the flattened PDF of the v1 document job. This protects detected text; it does not hide faces, barcodes, or non-text identifiers.

## Files and scans

The Files tool accepts UTF-8 TXT, PDF, DOCX, PNG, JPEG, and BMP, up to 10 MB. The **Output** menu selects the API:

- **API v2 · text and images** (default). TXT and images go to `POST /v2/protect`. An image comes back with every detected text line filled; the app shows it as the preview and keeps the protected text of the image for chat. Images are limited to 6 MiB and the pixel limit in `/v2/capabilities`. PDF and DOCX are uploaded to `/v2/uploads` and protected by a `/v2/jobs` document job, which returns protected text, entities, and the replacement map. API v2 document jobs do not return a redacted PDF yet.
- **API v1 · redacted PDF**. Every file goes through `/v1/documents/jobs`. Images are converted locally to image-only PDF. Outputs include protected text, a flattened redacted PDF, an optional private replacement map, and local page previews.

Jobs are submitted once with an `Idempotency-Key`, polled, and deleted after the outputs are downloaded (`DELETE /v2/jobs/{id}` also deletes the upload). The app never automatically retries an ambiguous billable request. The trace says whether remote cleanup was confirmed. ShinrAI's documented retention (24 hours for v2 jobs and uploads) still applies when cleanup cannot be confirmed.

## Azure comparison and API explorer

Azure comparison sends the same request to ShinrAI's Azure-compatible path and, optionally, a real Azure Language endpoint you configure. It reports each HTTP result and locally observed response time. Similarity is not presented as an accuracy benchmark.

The API Explorer lists the native API v2 first: detect, protect, protect with `mapping.known`, restore, capabilities, types, usage, and the job lifecycle. The native v1 routes follow. It also includes reviewed examples for Azure Language legacy, current, asynchronous, and conversation contracts; AWS Comprehend detection, contains-PII, credential, and configured S3 job operations; and Google DLP inspection, de-identification, image redaction, templates, and stored info types.

After connecting, the app supplements these examples with processing routes from the deployment's OpenAPI document. Only `/v1`, Azure Language, AWS credential, and `/v2` processing prefixes are accepted. Dashboard, account, OAuth, MCP, and operator routes cannot be called through the explorer.

## Diagnostics and privacy

The right-side trace records observed request and response bodies, returned headers, local end-to-end timing, server-reported timing when present, first-token time, usage, and cleanup. Credentials are always masked. **Export safe** also removes original content and restoration maps. **Export with private data** requires an explicit browser confirmation.

Session traces, attachments, mappings, and chat history are lost when the process stops. **Remember securely** uses macOS Keychain, Windows Credential Locker, or a supported Linux Secret Service. If no recommended credential store is available, the app continues in memory-only mode. Environment variables are also supported:

| Variable | Purpose |
| --- | --- |
| `SHINRAI_API_KEY` | ShinrAI application key |
| `SHINRAI_BASE_URL` | ShinrAI base URL |
| `HELLO_SHINRAI_LLM_API_KEY` | LLM provider key |
| `HELLO_SHINRAI_LLM_MODELS_URL` | Model catalogue URL |
| `HELLO_SHINRAI_LLM_INFERENCE_URL` | Chat or Responses URL |
| `HELLO_SHINRAI_LLM_MODEL` | Selected model |
| `AZURE_LANGUAGE_ENDPOINT` | Optional real Azure endpoint |
| `AZURE_LANGUAGE_KEY` | Optional real Azure key |
| `HELLO_SHINRAI_PORT` | Local port |

## Troubleshooting

- **Connection failed** means the endpoint could not be reached or authenticated. It is not displayed as a missing tier entitlement.
- **Real-time · fast says not entitled** means `/v2/capabilities` lists the tier as `not_in_plan` for that key (`allowed: false` from `/v1/models` on a v1-only deployment). Select Standard or Batch, or use a key with the required plan or pack.
- **This deployment does not serve API v2 document jobs** means `/v2/capabilities` lists no `file` jobs. Choose **API v1 · redacted PDF** for PDF and DOCX.
- **This ShinrAI deployment does not serve the native API v2** means the deployment answered 404 for `/v2/capabilities`. Text and Files still work with the v1 option; Chat needs API v2.
- **Model discovery failed** does not erase a manually entered model. Verify the models URL and key, or continue with the model ID from the provider's documentation.
- **Image or scan failed** can indicate disabled OCR, an unsupported document feature, low OCR confidence, page/pixel limits, or an unavailable document worker. The remote error appears in the trace.
- **Port unavailable** can be fixed with `hello-shinrai --port 8877`.
- **Browser did not open**: copy the local URL printed in the terminal. Use `--no-browser` on headless systems.
- **Local session expired** is recovered automatically after an app restart. If a tab was opened with v0.1.0, reload it once to load the recovery fix.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
pytest
hello-shinrai --no-browser
```

The frontend is bundled as package data under `src/hello_shinrai/static`; no Node.js step is required. `requirements.lock` pins the direct runtime dependencies used for the reviewed container build. The protection and restoration code is derived from Innovius's MIT-licensed `shinrai-connect` package; see `NOTICE` and `LICENSE`.
