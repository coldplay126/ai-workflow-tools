Write only `{file_name}` for `{service}/{domain}`.

This is a fan-out writer prompt. Your output must be internally correct and consistent with the shared domain model.

## Target File
- file: {file_name}
- title: {title}

## Stage 1 Memo
{stage1_memo}

## Domain XML Bundle
{domain_bundle}

Constraints:
- Stay consistent with the repository evidence in the bundle.
- Avoid inventing fields, endpoints, or integrations without support.
- Optimize this file for later synthesis with the other Stage 2 outputs.
