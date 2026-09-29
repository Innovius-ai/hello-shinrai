# Hello ShinrAI

Hello ShinrAI is a local playground for learning, testing, and debugging ShinrAI. It gives you a browser workspace for text protection, document and scan handling, protected LLM chat, Azure Language comparisons, and provider-compatible API calls. Optional local vendor endpoints let unchanged Azure, Google, and AWS SDKs call ShinrAI. The request trace shows what ShinrAI received, what the LLM received, and what was restored locally.

Hello ShinrAI uses the [ShinrAI native PII API v2](https://shinrai.innovius.io/public-docs/pii-api-v2.md). It needs a deployment that serves API v2. Azure compare and the API explorer also call the Azure, Google, and AWS compatibility APIs.

The app binds only to `127.0.0.1`. API keys remain in the local Python process unless you explicitly save them in your operating system's credential store. There is no database, frontend build, or local model download.

## Run it

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/) once, then run the tagged release:

```bash
uvx --from https://github.com/Innovius-ai/hello-shinrai/releases/download/v0.2.0/hello_shinrai-0.2.0-py3-none-any.whl hello-shinrai
```

The browser opens at `http://127.0.0.1:8765`. Use another port or skip browser opening when needed:

```bash
uvx --from https://github.com/Innovius-ai/hello-shinrai/releases/download/v0.2.0/hello_shinrai-0.2.0-py3-none-any.whl hello-shinrai --port 8877 --no-browser
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
docker run --rm -p 127.0.0.1:8765:8765 ghcr.io/innovius-ai/hello-shinrai:v0.2.0
```

## First request

1. Open **Connections** and enter your ShinrAI API key. The managed base URL defaults to `https://api.shinrai.innovius.io` and can be changed for sandbox or on-premises deployments.
2. Connect. Hello ShinrAI reads `GET /v2/capabilities` and `GET /v2/usage`, then shows available models, balance, and Standard, Batch, and Real-time access. Real-time is labelled as the fast tier. The app refuses a deployment that answers 404 or 501 for `/v2/capabilities`.
3. Open **Text**, keep the supplied synthetic example, and run protection.
4. Expand the trace on the right to inspect the request, response, request IDs, timing, findings, mapping, and usage.

A ShinrAI key is enough for Text, Files, Azure-shaped calls to ShinrAI, AWS Comprehend compatibility, and Google DLP compatibility. Chat additionally needs an LLM provider key.

## What each workspace calls

| Workspace | Native API v2 |
| --- | --- |
| Text · Detect only | `POST /v2/detect` |
| Text · Protect text, Consistent batch | `POST /v2/protect` with `policy.preset` and `output.include: ["entities", "mapping"]` |
| Text · Restore locally | No request: the last replacement map restores the text in this process |
| Text · Restore via /v2/restore | `POST /v2/restore` with `mapping.known` (free, sends the originals of the map) |
| Files · TXT | `POST /v2/protect` |
| Files · PNG, JPEG, BMP | `POST /v2/protect` with an image input: filled image, pixel boxes, mapping |
| Files · PDF, DOCX | `POST /v2/uploads`, `POST /v2/jobs` (`kind: document`), artifacts `protected` (redacted PDF), `text`, `entities`, `mapping`, then `DELETE /v2/jobs/{id}` |
| Chat | `POST /v2/protect` with earlier pairs in `mapping.known` |
| Connections | `GET /v2/capabilities`, `GET /v2/usage` |

The modes map to v2 presets: **Pseudonymize** is `pseudonymize`, **Mask** is `mask`, and **Label** is `label`. Pseudonymize and Label are reversible, so Text and Chat restore them locally. The model field accepts `latest` or a version such as `v1.4`. Without a custom confidence floor, the model's served floor applies.

## Protected chat

Open **Connections**, choose Innovius, OpenAI, Kie, or Custom, and configure the model-list URL, Chat Completions or Responses URL, corresponding key, and model ID.

Model discovery accepts common OpenAI-style `data` and `models` catalogues. A model appearing in a catalogue does not prove that it accepts Responses or image input. Kie uses model-specific Chat Completions URLs for some models; copy the endpoint shown in that model's Kie documentation. Kie's Responses-compatible Codex route uses `https://api.kie.ai/api/v1/responses`.

Every chat turn protects the complete local conversation with one `POST /v2/protect` before contacting the model. API v2 draws new surrogates for every request, so the app sends the pairs of earlier turns as `mapping.known`: a person keeps one stand-in for the whole conversation. Protection failure stops the request. The model sees replacements and the answer is restored locally, including replacements split across streamed chunks. The app asks supported providers not to store model responses and does not use hosted conversation state or execute tools.

Files can be sent as protected text. For vision-capable models, you can also send images that come exclusively from ShinrAI's output: the filled image, or the pages of the redacted PDF. This protects detected text; it does not hide faces, barcodes, or non-text identifiers.

## Files and scans

The Files tool accepts UTF-8 TXT, PDF, DOCX, PNG, JPEG, and BMP, up to 10 MB.

- **TXT and images** go to `POST /v2/protect`. An image comes back with every detected text line filled. The app shows it as the preview and keeps the protected text of the image for chat. Images are limited to 6 MiB and to the pixel limit in `/v2/capabilities`. Send larger scans as a PDF.
- **PDF and DOCX** go to `/v2/uploads` and run as a `/v2/jobs` document job. The job returns the redacted PDF (`protected`, `application/pdf`), the protected text (`text`), the findings (`entities`), and the replacement map (`mapping`). The app renders local page previews of the redacted PDF for up to 8 pages. Longer documents keep the PDF download without previews.
- **Older deployments** refuse the `text` artifact and return the protected text as `protected`. The app then submits the job once more without `text` and shows the protected text without a redacted PDF or page previews. The attachment says so.

Jobs are submitted with an `Idempotency-Key`, polled, and deleted after the outputs are downloaded (`DELETE /v2/jobs/{id}` also deletes the upload). The app never automatically retries an ambiguous billable request. It submits a document job a second time only after the deployment refused the artifact list, before it created a job. The trace says whether remote cleanup was confirmed. ShinrAI's documented retention (24 hours for v2 jobs and uploads) still applies when cleanup cannot be confirmed.

## Azure comparison and API explorer

Azure comparison sends the same request to ShinrAI's Azure-compatible path and, optionally, a real Azure Language endpoint you configure. It reports each HTTP result and locally observed response time. Similarity is not presented as an accuracy benchmark.

The API Explorer lists the native API v2 first: detect, protect, protect with `mapping.known`, restore, capabilities, types, usage, and the job lifecycle. It also includes reviewed examples for Azure Language legacy, current, asynchronous, and conversation contracts; AWS Comprehend detection, contains-PII, credential, and configured S3 job operations; and Google DLP inspection, de-identification, image redaction, templates, and stored info types.

After connecting, the app supplements these examples with processing routes from the deployment's OpenAPI document. Only `/v2`, `/v1/azure`, `/v1/google`, `/v1/aws`, Azure Language, and AWS credential prefixes are accepted. Dashboard, account, OAuth, MCP, and operator routes cannot be called through the explorer.

## Vendor endpoints

Vendor endpoints let unchanged Azure, Google, and AWS SDKs call ShinrAI through this app. Only the endpoint in the SDK changes. The endpoints are off by default.

Start them in **Connections → Vendor endpoints**, or start the app with `hello-shinrai --vendor-ports`. Three listeners open on `127.0.0.1`:

| Port | Vendor API | ShinrAI destination | Credential that the app adds |
| --- | --- | --- | --- |
| 8210 | Azure AI Language PII | `<base URL>/v1/azure` | `Ocp-Apim-Subscription-Key` with your ShinrAI key |
| 8211 | Google Cloud DLP | `<base URL>/v1/google` | `x-goog-api-key` with your ShinrAI key |
| 8212 | AWS Comprehend PII | `<base URL>/v1/aws/` | A new AWS Signature Version 4 with the key pair that ShinrAI issues for your key |

`--vendor-ports-base 8310` moves the three listeners to ports 8310, 8311, and 8312. Each endpoint URL has a random path, for example `http://127.0.0.1:8210/p/3f9c0a5e71d24b86`. The path changes with every run. Copy the URLs from **Connections**.

The app removes the credentials that the SDK sends, so the SDK can use any placeholder key. For AWS, the app gets the key pair once from `POST /providers/aws/credentials` and signs for region `eu-central-1` and service `comprehend`. Azure `Operation-Location` headers and `nextLink` values point back to the local endpoint, so SDK pollers and pagers work.

```python
# Azure AI Language (azure-ai-textanalytics)
client = TextAnalyticsClient("http://127.0.0.1:8210/p/<run path>", AzureKeyCredential("placeholder"))

# Google Cloud DLP (google-cloud-dlp): use the REST transport
client = dlp_v2.DlpServiceClient(
    transport="rest", client_options={"api_endpoint": "http://127.0.0.1:8211/p/<run path>", "api_key": "placeholder"}
)

# AWS Comprehend (boto3)
comprehend = boto3.client(
    "comprehend", endpoint_url="http://127.0.0.1:8212/p/<run path>", region_name="eu-central-1",
    aws_access_key_id="placeholder", aws_secret_access_key="placeholder",
)
```

- **While the endpoints run, any program on this computer that has an endpoint URL can use your ShinrAI key.** Stop the endpoints when you do not need them.
- The listeners accept requests for `127.0.0.1` and `localhost` only. Requests without the path of the run get 404. Requests from web pages (with an `Origin` or `Sec-Fetch-Site` header) are refused, and the responses carry no CORS headers.
- The listeners speak HTTP/1.1. Google gRPC clients are not supported; use the REST transport.
- The trace records the method, path, status, time, and vendor headers of each call. It never records request or response bodies.
- The endpoints stop when the app stops. The setting is never saved. In the Docker image the endpoints listen on the loopback address of the container, so the host cannot reach them.

### Exports

**Connections → Vendor endpoints** also exports each vendor API:

- **OpenAPI**: the vendor operations of the connected deployment's `/openapi.json`, at the vendor's own paths. While the endpoints run, the server is the local endpoint and the document has no security requirements. Otherwise the server is `<base URL>/v1/<vendor>`. A running endpoint also serves this document at `<endpoint>/openapi.json`.
- **.http**: requests for the VS Code REST Client and the JetBrains HTTP Client. For the deployment, the requests use the `{{SHINRAI_API_KEY}}` variable of your HTTP client environment.
- **curl**: a shell file with one function per request. For the deployment, it reads the key from the `SHINRAI_API_KEY` environment variable. AWS requests use `curl --aws-sigv4` (curl 8 or later) with the pair from the `aws_credentials` function.

Exports never contain your key.

## Diagnostics and privacy

The right-side trace records observed request and response bodies, returned headers, local end-to-end timing, server-reported timing when present, first-token time, usage, and cleanup. Credentials are always masked. **Export safe** also removes original content, uploaded file names, and restoration maps. **Export with private data** requires an explicit browser confirmation.

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

Command-line options: `--port`, `--no-browser`, `--vendor-ports`, and `--vendor-ports-base` (default 8210).

## Troubleshooting

- **Connection failed** means the endpoint could not be reached or authenticated. It is not displayed as a missing tier entitlement.
- **Real-time · fast says not entitled** means `/v2/capabilities` lists the tier as `not_in_plan` for that key. Select Standard or Batch, or use a key with the required plan or pack.
- **This deployment does not serve API v2 document jobs** means `/v2/capabilities` lists no `file` jobs. Send text files and images, or use a deployment with document jobs for PDF and DOCX.
- **This deployment does not serve the ShinrAI API v2** means the deployment answered 404 or 501 for `/v2/capabilities`. Use the hosted API or an offline release that serves API v2. With an offline image that serves only API v1, use Hello ShinrAI release 0.1.4.
- **No redacted PDF for a document** means the deployment returned the protected text only. The text, findings, and replacement map are complete.
- **Model discovery failed** does not erase a manually entered model. Verify the models URL and key, or continue with the model ID from the provider's documentation.
- **Image or scan failed** can indicate disabled OCR, an unsupported document feature, low OCR confidence, page/pixel limits, or an unavailable document worker. The remote error appears in the trace.
- **Port unavailable** can be fixed with `hello-shinrai --port 8877`. For the vendor endpoints, use `--vendor-ports-base 8310` or change **First port** in Connections.
- **Vendor endpoint answers 404** means the request did not use the endpoint URL of this run, or the path belongs to another vendor. Copy the URL from Connections again after a restart.
- **Vendor endpoint answers 503** means Hello ShinrAI has no ShinrAI key. Connect ShinrAI first.
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
