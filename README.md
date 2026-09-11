# Hello ShinrAI

Hello ShinrAI is a local playground for learning, testing, and debugging ShinrAI. It gives you a browser workspace for text protection, document and scan handling, protected LLM chat, Azure Language comparisons, and provider-compatible API calls. The request trace shows what ShinrAI received, what the LLM received, and what was restored locally.

The app binds only to `127.0.0.1`. API keys remain in the local Python process unless you explicitly save them in your operating system's credential store. There is no database, frontend build, or local model download.

## Run it

Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/) once, then run the tagged release:

```bash
uvx --from https://github.com/Innovius-ai/hello-shinrai/releases/download/v0.1.1/hello_shinrai-0.1.1-py3-none-any.whl hello-shinrai
```

The browser opens at `http://127.0.0.1:8765`. Use another port or skip browser opening when needed:

```bash
uvx --from https://github.com/Innovius-ai/hello-shinrai/releases/download/v0.1.1/hello_shinrai-0.1.1-py3-none-any.whl hello-shinrai --port 8877 --no-browser
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
docker run --rm -p 127.0.0.1:8765:8765 ghcr.io/innovius-ai/hello-shinrai:v0.1.1
```

## First request

1. Open **Connections** and enter your ShinrAI API key. The managed base URL defaults to `https://api.shinrai.innovius.io` and can be changed for sandbox or on-premises deployments.
2. Connect. Hello ShinrAI reads `/v1/models` and `/v1/usage`, then shows available models, balance, and Standard, Batch, and Real-time access. Real-time is labelled as the fast tier.
3. Open **Text**, keep the supplied synthetic example, and run protection.
4. Expand the trace on the right to inspect the request, response, request IDs, timing, findings, mapping, and usage.

A ShinrAI key is enough for Text, Files, Azure-shaped calls to ShinrAI, AWS Comprehend compatibility, and Google DLP compatibility. Chat additionally needs an LLM provider key.

## Protected chat

Open **Connections**, choose Innovius, OpenAI, Kie, or Custom, and configure the model-list URL, Chat Completions or Responses URL, corresponding key, and model ID.

Model discovery accepts common OpenAI-style `data` and `models` catalogues. A model appearing in a catalogue does not prove that it accepts Responses or image input. Kie uses model-specific Chat Completions URLs for some models; copy the endpoint shown in that model's Kie documentation. Kie's Responses-compatible Codex route uses `https://api.kie.ai/api/v1/responses`.

Every chat turn protects the complete local conversation before contacting the model. Protection failure stops the request. With replacement mode, the model sees replacements and the answer is restored locally, including replacements split across streamed chunks. The app asks supported providers not to store model responses and does not use hosted conversation state or execute tools.

Files can be sent as protected text. For vision-capable models, you can also send page images rendered exclusively from ShinrAI's protected, flattened PDF. This protects detected text; it does not hide faces, barcodes, or non-text identifiers.

## Files and scans

The Files tool accepts UTF-8 TXT, PDF, DOCX, PNG, JPEG, and BMP, up to 10 MB. Images are converted locally to image-only PDF and passed through the same ShinrAI document job. Successful outputs include protected text, a flattened redacted PDF, optional private replacement map, and local page previews.

The app polls one idempotent job and requests deletion after downloading its outputs. It never automatically retries an ambiguous billable request. The trace says whether remote cleanup was confirmed. ShinrAI's documented retention still applies when cleanup cannot be confirmed.

## Azure comparison and API explorer

Azure comparison sends the same request to ShinrAI's Azure-compatible path and, optionally, a real Azure Language endpoint you configure. It reports each HTTP result and locally observed response time. Similarity is not presented as an accuracy benchmark.

The API Explorer includes reviewed examples for ShinrAI native analysis and protection; Azure Language legacy, current, asynchronous, and conversation contracts; AWS Comprehend detection, contains-PII, credential, and configured S3 job operations; and Google DLP inspection, de-identification, image redaction, templates, and stored info types.

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
- **Real-time · fast says not entitled** means `/v1/models` returned `allowed: false` for that key. Select Standard or Batch, or use a key with the required plan or pack.
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
