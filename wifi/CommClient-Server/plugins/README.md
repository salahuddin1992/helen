# Helen Plugin Development Guide

This directory holds **example plugins** and a brief development guide.
Production plugins live in the database (`plugin_manifests`) and are
loaded by `app.services.plugins.loader` — anything in this folder is
considered example/reference material.

## Plugin Lifecycle

```
plugin.json (manifest)  →  validated by manifest_schema.py
                        →  signature verified against trust store
                        →  PluginManifest row created
                        →  PluginInstallation per workspace
                        →  hooks registered → invoked on events
```

## Manifest fields (v1.0)

| Field               | Required | Type                  | Notes                                |
|---------------------|----------|-----------------------|--------------------------------------|
| `slug`              | yes      | string (a-z 0-9 _ -) | unique global ID                     |
| `name`              | yes      | string                | UI title                             |
| `version`           | yes      | semver                | matches install records              |
| `entrypoint`        | yes      | string                | path within plugin bundle            |
| `permissions`       | no       | string[]              | see `manifest_schema.ALLOWED_PERMISSIONS` |
| `hooks_subscribed`  | no       | string[]              | see `manifest_schema.ALLOWED_HOOKS`  |
| `min_helen_version` | no       | semver                | inclusive                            |
| `max_helen_version` | no       | semver                | inclusive                            |
| `code_url`          | no       | URL                   | https or file://                     |
| `code_sha256`       | no       | hex                   | integrity check                      |
| `signature`         | no       | base64                | Ed25519 over code_sha256             |
| `signed_by`         | no       | string                | trust-store key name                 |

## Sandbox

Plugin code is executed under `app.services.plugins.sandbox`:

* `RestrictedPython` when available, AST-denylist otherwise
* allowed imports: `json`, `math`, `re`, `datetime`, `collections`,
  `itertools`, `hashlib`, `uuid`, `helen_sdk`
* forbidden: `os`, `subprocess`, `sys`, `socket`, `open`, `eval`,
  `exec`, `compile`, `__import__`
* CPU cap: 5 seconds (configurable per call)
* memory cap: 128 MiB (Unix only)
* stdout truncated to 1 MiB

## helen_sdk surface

```python
import helen_sdk

helen_sdk.send_message(channel_id="...", content="hi")
helen_sdk.get_user(user_id)
helen_sdk.get_channel(channel_id)
helen_sdk.kv_set("counter", 1)
helen_sdk.kv_get("counter", default=0)
helen_sdk.http_request(method="POST", url="https://hooks.slack.com/...",
                       json_body={"text": "hi"})
```

Each call enforces the installation's granted permissions.

## Hook examples

Hooks must be top-level functions whose name matches the hook key:

```python
def on_message_created(payload):
    import helen_sdk
    if "hello" in payload.get("content", "").lower():
        helen_sdk.send_message(
            channel_id=payload["channel_id"],
            content="Hello back!",
        )
```

## Signing

```bash
# generate via admin API
curl -X POST -H "Authorization: Bearer $TOK" /api/admin/plugins/keypairs

# sign code_sha256
python -c "from app.services.plugins.signer import sign_payload; print(sign_payload(b'<sha256>', '<priv>'))"
```

## Marketplace

* Public catalogue: `GET /api/plugins/marketplace`
* Install: `POST /api/plugins/install { slug, version }`
* Marketplace listings are reviewed via the admin module
  (`/api/admin/plugins/manifests/{id}/approve`).
