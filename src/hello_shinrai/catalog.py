from __future__ import annotations

from typing import Any

from .client import vendor_path

V2 = "ShinrAI native API v2"
SAMPLE = "Email Ada Lovelace at ada@example.org. She lives in London."

# The native API v2 comes first; the vendor compatibility surfaces follow.
NATIVE_V2: list[dict[str, Any]] = [
    {
        "id": "v2.detect",
        "group": V2,
        "name": "Detect personal data",
        "method": "POST",
        "path": "/v2/detect",
        "auth": "bearer",
        "body": {"text": SAMPLE, "language": "en"},
    },
    {
        "id": "v2.protect",
        "group": V2,
        "name": "Protect text (pseudonymize, keep the mapping)",
        "method": "POST",
        "path": "/v2/protect",
        "auth": "bearer",
        "body": {
            "text": SAMPLE,
            "policy": {"preset": "pseudonymize"},
            "output": {"include": ["entities", "mapping"]},
        },
    },
    {
        "id": "v2.protect-batch",
        "group": V2,
        "name": "Protect several texts with one map",
        "method": "POST",
        "path": "/v2/protect",
        "auth": "bearer",
        "body": {
            "texts": ["Ada Lovelace lives in London.", "Email Ada Lovelace at ada@example.org."],
            "policy": {"preset": "label"},
            "output": {"include": ["entities", "mapping"]},
        },
    },
    {
        "id": "v2.protect-known",
        "group": V2,
        "name": "Protect with earlier pairs (mapping.known)",
        "method": "POST",
        "path": "/v2/protect",
        "auth": "bearer",
        "body": {
            "text": "Ada Lovelace called again from London.",
            "mapping": {"known": [{"original": "Ada Lovelace", "replacement": "Grace Palmer"}]},
            "output": {"include": ["entities", "mapping"]},
        },
    },
    {
        "id": "v2.restore",
        "group": V2,
        "name": "Restore surrogates (free)",
        "method": "POST",
        "path": "/v2/restore",
        "auth": "bearer",
        "body": {
            "mapping": {"known": [{"original": "Ada Lovelace", "replacement": "Grace Palmer"}]},
            "inputs": [{"id": "1", "text": "Grace Palmer replied."}],
        },
    },
    {
        "id": "v2.capabilities",
        "group": V2,
        "name": "Capabilities, tiers and limits",
        "method": "GET",
        "path": "/v2/capabilities",
        "auth": "bearer",
        "body": None,
    },
    {
        "id": "v2.types",
        "group": V2,
        "name": "Entity types and vendor vocabularies",
        "method": "GET",
        "path": "/v2/types",
        "auth": "bearer",
        "body": None,
    },
    {
        "id": "v2.usage",
        "group": V2,
        "name": "Balance and last 30 days",
        "method": "GET",
        "path": "/v2/usage",
        "auth": "bearer",
        "body": None,
    },
    {
        "id": "v2.job-create",
        "group": V2,
        "name": "Start a text batch job",
        "method": "POST",
        "path": "/v2/jobs",
        "auth": "bearer",
        "body": {
            "kind": "text_batch",
            "inputs": [
                {"id": "1", "kind": "text", "text": "Ada Lovelace lives in London."},
                {"id": "2", "kind": "text", "text": "Email ada@example.org."},
            ],
            "output": {"artifacts": ["protected", "entities"]},
        },
    },
    {
        "id": "v2.job-list",
        "group": V2,
        "name": "List jobs",
        "method": "GET",
        "path": "/v2/jobs",
        "auth": "bearer",
        "body": None,
    },
    {
        "id": "v2.job-poll",
        "group": V2,
        "name": "Poll a job",
        "method": "GET",
        "path": "/v2/jobs/{job_id}",
        "auth": "bearer",
        "body": None,
        "requires": "Replace {job_id}",
    },
    {
        "id": "v2.job-artifact",
        "group": V2,
        "name": "Download a job artifact",
        "method": "GET",
        "path": "/v2/jobs/{job_id}/artifacts/{name}",
        "auth": "bearer",
        "body": None,
        "requires": "Replace {job_id} and {name} (protected, text, entities, or mapping); use Download response",
    },
    {
        "id": "v2.job-cancel",
        "group": V2,
        "name": "Cancel a job",
        "method": "POST",
        "path": "/v2/jobs/{job_id}/cancel",
        "auth": "bearer",
        "body": None,
        "requires": "Replace {job_id}",
    },
    {
        "id": "v2.job-delete",
        "group": V2,
        "name": "Delete a job, its upload and artifacts",
        "method": "DELETE",
        "path": "/v2/jobs/{job_id}",
        "auth": "bearer",
        "body": None,
        "requires": "Replace {job_id}",
    },
]

OPERATIONS: list[dict[str, Any]] = [
    *NATIVE_V2,
    {
        "id": "azure.legacy",
        "group": "Azure Language",
        "name": "Legacy PII recognition",
        "method": "POST",
        "path": "/text/analytics/v3.1/entities/recognition/pii",
        "query": {},
        "auth": "azure",
        "body": {"documents": [{"id": "1", "language": "en", "text": "Email Ada at ada@example.org"}]},
    },
    {
        "id": "azure.sync",
        "group": "Azure Language",
        "name": "Analyze text PII",
        "method": "POST",
        "path": "/language/:analyze-text",
        "query": {"api-version": "2026-05-01"},
        "auth": "azure",
        "body": {
            "kind": "PiiEntityRecognition",
            "analysisInput": {"documents": [{"id": "1", "language": "en", "text": "Email Ada at ada@example.org"}]},
            "parameters": {"modelVersion": "latest"},
        },
    },
    {
        "id": "azure.async",
        "group": "Azure Language",
        "name": "Submit asynchronous text job",
        "method": "POST",
        "path": "/language/analyze-text/jobs",
        "query": {"api-version": "2026-05-01"},
        "auth": "azure",
        "body": {
            "displayName": "Hello ShinrAI",
            "analysisInput": {"documents": [{"id": "1", "language": "en", "text": "Email Ada at ada@example.org"}]},
            "tasks": [{"kind": "PiiEntityRecognition", "taskName": "PII", "parameters": {"modelVersion": "latest"}}],
        },
    },
    {
        "id": "azure.conversation",
        "group": "Azure Language",
        "name": "Submit conversation PII job",
        "method": "POST",
        "path": "/language/analyze-conversations/jobs",
        "query": {"api-version": "2024-11-01"},
        "auth": "azure",
        "body": {
            "displayName": "Support call",
            "analysisInput": {
                "conversations": [
                    {
                        "id": "call-1",
                        "language": "en",
                        "modality": "text",
                        "domain": "generic",
                        "conversationItems": [
                            {
                                "id": "turn-1",
                                "participantId": "caller",
                                "role": "customer",
                                "text": "Email ada@example.org",
                            }
                        ],
                    }
                ]
            },
            "tasks": [{"kind": "ConversationalPIITask", "taskName": "PII", "parameters": {"piiCategories": ["All"]}}],
        },
    },
    {
        "id": "aws.credentials",
        "group": "AWS Comprehend",
        "name": "Issue SDK credentials",
        "method": "POST",
        "path": "/providers/aws/credentials",
        "auth": "bearer",
        "body": {},
    },
    {
        "id": "aws.detect",
        "group": "AWS Comprehend",
        "name": "Detect PII entities",
        "method": "POST",
        "path": "/",
        "auth": "aws",
        "aws_target": "Comprehend_20171127.DetectPiiEntities",
        "body": {"Text": "Email Ada at ada@example.org", "LanguageCode": "en"},
    },
    {
        "id": "aws.contains",
        "group": "AWS Comprehend",
        "name": "Contains PII entities",
        "method": "POST",
        "path": "/",
        "auth": "aws",
        "aws_target": "Comprehend_20171127.ContainsPiiEntities",
        "body": {"Text": "Email Ada at ada@example.org", "LanguageCode": "en"},
    },
    {
        "id": "aws.start-job",
        "group": "AWS Comprehend",
        "name": "Start S3 PII job",
        "method": "POST",
        "path": "/",
        "auth": "aws",
        "aws_target": "Comprehend_20171127.StartPiiEntitiesDetectionJob",
        "requires": "Operator-configured S3 binding",
        "body": {
            "InputDataConfig": {"S3Uri": "s3://allowed-input/example.txt", "InputFormat": "ONE_DOC_PER_LINE"},
            "OutputDataConfig": {"S3Uri": "s3://allowed-output/"},
            "Mode": "ONLY_REDACTION",
            "RedactionConfig": {"PiiEntityTypes": ["ALL"], "MaskMode": "REPLACE_WITH_PII_ENTITY_TYPE"},
            "DataAccessRoleArn": "arn:aws:iam::000000000000:role/shinrai",
            "LanguageCode": "en",
        },
    },
    {
        "id": "google.inspect",
        "group": "Google DLP",
        "name": "Inspect content",
        "method": "POST",
        "path": "/v2/projects/hello/locations/global/content:inspect",
        "auth": "google",
        "body": {"item": {"value": "Email Ada at ada@example.org"}, "inspectConfig": {"includeQuote": True}},
    },
    {
        "id": "google.deidentify",
        "group": "Google DLP",
        "name": "De-identify content",
        "method": "POST",
        "path": "/v2/projects/hello/locations/global/content:deidentify",
        "auth": "google",
        "body": {
            "item": {"value": "Email Ada at ada@example.org"},
            "deidentifyConfig": {
                "infoTypeTransformations": {
                    "transformations": [{"primitiveTransformation": {"replaceWithInfoTypeConfig": {}}}]
                }
            },
        },
    },
    {
        "id": "google.image",
        "group": "Google DLP",
        "name": "Redact image",
        "method": "POST",
        "path": "/v2/projects/hello/locations/global/image:redact",
        "auth": "google",
        "requires": "OCR enabled on this deployment; replace byteItem.data with base64 image bytes",
        "body": {"byteItem": {"type": "IMAGE_PNG", "data": "BASE64_IMAGE"}, "includeFindings": True},
    },
    {
        "id": "google.inspect-template-list",
        "group": "Google DLP",
        "name": "List inspect templates",
        "method": "GET",
        "path": "/v2/projects/hello/locations/global/inspectTemplates",
        "auth": "google",
        "body": None,
    },
    {
        "id": "google.inspect-template-create",
        "group": "Google DLP",
        "name": "Create inspect template",
        "method": "POST",
        "path": "/v2/projects/hello/locations/global/inspectTemplates",
        "auth": "google",
        "body": {"inspectTemplate": {"displayName": "Hello ShinrAI", "inspectConfig": {"includeQuote": True}}},
    },
    {
        "id": "google.stored-type-list",
        "group": "Google DLP",
        "name": "List stored info types",
        "method": "GET",
        "path": "/v2/projects/hello/locations/global/storedInfoTypes",
        "auth": "google",
        "body": None,
    },
]

# Lifecycle operations use visible placeholders. The explorer requires users to
# replace them, then checks that the edited path still matches this catalogue.
OPERATIONS.extend(
    [
        {
            "id": "azure.text-poll",
            "group": "Azure Language",
            "name": "Poll asynchronous text job",
            "method": "GET",
            "path": "/language/analyze-text/jobs/{job_id}",
            "query": {"api-version": "2026-05-01"},
            "auth": "azure",
            "body": None,
            "requires": "Replace {job_id}",
        },
        {
            "id": "azure.text-cancel",
            "group": "Azure Language",
            "name": "Cancel asynchronous text job",
            "method": "POST",
            "path": "/language/analyze-text/jobs/{job_id}:cancel",
            "query": {"api-version": "2026-05-01"},
            "auth": "azure",
            "body": None,
            "requires": "Replace {job_id}",
        },
        {
            "id": "azure.conversation-poll",
            "group": "Azure Language",
            "name": "Poll conversation job",
            "method": "GET",
            "path": "/language/analyze-conversations/jobs/{job_id}",
            "query": {"api-version": "2024-11-01"},
            "auth": "azure",
            "body": None,
            "requires": "Replace {job_id}",
        },
        {
            "id": "azure.conversation-cancel",
            "group": "Azure Language",
            "name": "Cancel conversation job",
            "method": "POST",
            "path": "/language/analyze-conversations/jobs/{job_id}:cancel",
            "query": {"api-version": "2024-11-01"},
            "auth": "azure",
            "body": None,
            "requires": "Replace {job_id}",
        },
        {
            "id": "azure.document-submit",
            "group": "Azure Language",
            "name": "Submit storage-backed document job",
            "method": "POST",
            "path": "/language/analyze-documents/jobs",
            "query": {"api-version": "2026-05-01"},
            "auth": "azure",
            "requires": "Replace the source and target with separately configured Azure Storage URLs",
            "body": {
                "analysisInput": {
                    "documents": [
                        {
                            "id": "file-1",
                            "source": {"kind": "AzureBlob", "location": "https://storage.example/input.txt?SIGNED"},
                            "target": {"kind": "AzureContainer", "location": "https://storage.example/output?SIGNED"},
                            "language": "en",
                        }
                    ]
                },
                "tasks": [{"kind": "PiiEntityRecognition", "taskName": "PII", "parameters": {}}],
            },
        },
        {
            "id": "azure.document-poll",
            "group": "Azure Language",
            "name": "Poll document job",
            "method": "GET",
            "path": "/language/analyze-documents/jobs/{job_id}",
            "query": {"api-version": "2026-05-01"},
            "auth": "azure",
            "body": None,
            "requires": "Replace {job_id}",
        },
        {
            "id": "azure.document-cancel",
            "group": "Azure Language",
            "name": "Cancel document job",
            "method": "POST",
            "path": "/language/analyze-documents/jobs/{job_id}:cancel",
            "query": {"api-version": "2026-05-01"},
            "auth": "azure",
            "body": None,
            "requires": "Replace {job_id}",
        },
        {
            "id": "azure.document-artifacts",
            "group": "Azure Language",
            "name": "List document artifacts",
            "method": "GET",
            "path": "/providers/azure/document-jobs/{job_id}/artifacts",
            "auth": "bearer",
            "body": None,
            "requires": "Replace {job_id}",
        },
        {
            "id": "azure.document-download",
            "group": "Azure Language",
            "name": "Download document artifact",
            "method": "GET",
            "path": "/providers/azure/document-jobs/{job_id}/artifacts/{artifact_id}",
            "auth": "bearer",
            "body": None,
            "requires": "Replace both identifiers; use Download response",
        },
        {
            "id": "aws.describe-job",
            "group": "AWS Comprehend",
            "name": "Describe S3 PII job",
            "method": "POST",
            "path": "/",
            "auth": "aws",
            "aws_target": "Comprehend_20171127.DescribePiiEntitiesDetectionJob",
            "body": {"JobId": "REPLACE_JOB_ID"},
        },
        {
            "id": "aws.list-jobs",
            "group": "AWS Comprehend",
            "name": "List S3 PII jobs",
            "method": "POST",
            "path": "/",
            "auth": "aws",
            "aws_target": "Comprehend_20171127.ListPiiEntitiesDetectionJobs",
            "body": {},
        },
        {
            "id": "aws.stop-job",
            "group": "AWS Comprehend",
            "name": "Stop S3 PII job",
            "method": "POST",
            "path": "/",
            "auth": "aws",
            "aws_target": "Comprehend_20171127.StopPiiEntitiesDetectionJob",
            "body": {"JobId": "REPLACE_JOB_ID"},
        },
        {
            "id": "google.inspect-template-get",
            "group": "Google DLP",
            "name": "Get inspect template",
            "method": "GET",
            "path": "/v2/projects/{project}/locations/{location}/inspectTemplates/{template_id}",
            "auth": "google",
            "body": None,
            "requires": "Replace project, location, and template_id",
        },
        {
            "id": "google.inspect-template-delete",
            "group": "Google DLP",
            "name": "Delete inspect template",
            "method": "DELETE",
            "path": "/v2/projects/{project}/locations/{location}/inspectTemplates/{template_id}",
            "auth": "google",
            "body": None,
            "requires": "Replace project, location, and template_id",
        },
        {
            "id": "google.stored-type-create",
            "group": "Google DLP",
            "name": "Create dictionary / stored info type",
            "method": "POST",
            "path": "/v2/projects/{project}/locations/{location}/storedInfoTypes",
            "auth": "google",
            "requires": "Replace project and location",
            "body": {
                "storedInfoType": {
                    "displayName": "Hello dictionary",
                    "largeCustomDictionary": {"outputPath": {"path": "gs://configured-output/"}},
                }
            },
        },
    ]
)


BY_ID = {item["id"]: item for item in OPERATIONS}


def public_catalog(discovered: set[tuple[str, str]]) -> list[dict[str, Any]]:
    output = []
    for item in OPERATIONS:
        row = dict(item)
        if row["auth"] == "aws" and row["path"] == "/":
            row["availability"] = "catalogued"
        elif not discovered:
            row["availability"] = "not yet verified"
        elif any(
            route_method == row["method"] and template_matches(route, vendor_path(row["path"]))
            for route_method, route in discovered
        ):
            row["availability"] = "available"
        else:
            row["availability"] = "unavailable on this deployment"
        output.append(row)
    return output


def template_matches(template: str, path: str) -> bool:
    left, right = template.strip("/").split("/"), path.strip("/").split("/")
    return len(left) == len(right) and all(
        a == b or (a.startswith("{") and a.endswith("}")) for a, b in zip(left, right)
    )
