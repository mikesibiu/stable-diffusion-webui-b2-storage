# Stable Diffusion WebUI Backblaze B2 Cloud Storage Extension

This extension automatically uploads all generated images from the [Stable Diffusion WebUI (AUTOMATIC1111)](https://github.com/AUTOMATIC1111/stable-diffusion-webui) to your **Backblaze B2 Cloud Storage** bucket, with options for the native REST API, the S3-Compatible API, and automatic local disk cleanup.

> Works with **Backblaze B2 or any S3-compatible storage**: the `s3` API type speaks the standard S3 protocol with a configurable endpoint — just point the endpoint setting at your provider.

---

## Features

* **Auto-Upload:** Automatically intercepts generated images at the `on_image_saved` hook.
* **Non-Blocking:** Uploads run on a background worker thread, so image generation never waits on the network — batch generations queue up and upload while you keep working.
* **Dual Protocols:** Choose between Backblaze B2's native REST API (lightweight, zero extra dependencies) or the standard S3-Compatible API (via `boto3`), which works with any S3-compatible provider.
* **Restricted Keys Supported:** Works with application keys scoped to a single bucket (the recommended setup) — no account-wide `listBuckets` permission required.
* **Follows the [B2 Integration Checklist](https://www.backblaze.com/docs/cloud-storage-integration-checklist):**
  * Upload URL/token pairs are reused across files and only refreshed after a failure.
  * Connection failures, 408/429, and 5xx responses are retried with a fresh upload URL, honoring `Retry-After` and using exponential backoff (1s, 2s, ...); non-retryable errors like 403 (account cap reached) fail immediately with a clear message.
  * Expired auth tokens trigger automatic re-authentication, and the authenticated session is reused across uploads instead of re-authenticating per image.
  * All requests identify the integration via `User-Agent`, and uploads set `X-Bz-Info-src_last_modified_millis` from the source file.
* **Auto-Cleanup:** Option to delete local copies of images after successful upload, preventing your server or local machine from running out of disk space.
* **Folder Structure Preservation:** Uploads preserve the relative directory path of the WebUI outputs (e.g. `outputs/txt2img-images/2026-07-09/00001.png`).
* **Env-Var Credentials:** Credentials can be supplied via environment variables instead of the Settings UI, keeping secrets out of `config.json`.

---

## Installation

1. Copy or clone this extension directory into the `extensions/` directory of your Stable Diffusion WebUI:
   ```bash
   stable-diffusion-webui/extensions/stable-diffusion-webui-b2-storage/
   ```
2. Restart the Stable Diffusion WebUI.
3. **(S3 API type only)** The native API needs no extra dependencies. If you plan to use the S3-Compatible API, install `boto3` into the WebUI's Python environment:
   ```bash
   pip install boto3
   ```

---

## Creating a B2 Bucket and Application Key

**The bucket must already exist — the extension will not create it.** Create a (private) bucket in your Backblaze account first. If the configured bucket doesn't exist, uploads are skipped with a clear `Bucket 'name' not found` message in the log; images are still saved locally and generation is never interrupted.

Then create an application key **restricted to that bucket** with read/write capabilities. Single-bucket keys are fully supported and recommended — if the extension is pointed at a bucket the key cannot access, the log will say so explicitly.

---

## Configuration

1. In the WebUI, navigate to the **Settings** tab.
2. Select **Backblaze B2 Storage** on the left menu.
3. Configure the settings:
   * **Enable Backblaze B2 Uploads:** Check to enable.
   * **B2 API Type:** Select `native` or `s3`.
   * **B2 Key ID / S3 Access Key:** Your Backblaze application Key ID.
   * **B2 Application Key / S3 Secret Key:** Your Backblaze application key (masked in the UI).
   * **B2 Bucket Name:** Name of an existing B2 bucket (the extension does not create buckets).
   * **B2 S3 Endpoint:** Required only if using the `s3` API type (e.g., `https://s3.us-west-004.backblazeb2.com`).
   * **Delete local image copy after successful B2 upload:** Check to delete the local file after it has been safely pushed to the cloud. *See caveats below.*
4. Click **Apply settings** at the top of the Settings page.
5. Generate an image! Upload progress appears in your WebUI terminal, prefixed with `[B2 Storage Extension]`.

### Environment variables

Any credential field left **empty** in the Settings UI falls back to an environment variable. This keeps secrets out of the WebUI's plaintext `config.json`:

| Setting | Environment variable |
|---|---|
| B2 Key ID | `B2_APPLICATION_KEY_ID` |
| B2 Application Key | `B2_APPLICATION_KEY` |
| B2 Bucket Name | `B2_BUCKET` |
| B2 S3 Endpoint | `B2_S3_ENDPOINT` |

`B2_APPLICATION_KEY_ID` / `B2_APPLICATION_KEY` are the same names the official Backblaze CLI uses.

### Caveats for "Delete local image copy"

Deletion happens in the background right after the upload succeeds. Because the WebUI serves gallery images and PNG-info lookups from the local output files, deleting them means:

* the image may no longer render in the output gallery after a page reload;
* "Open images output directory" and PNG Info on the saved file won't find it.

Use this option on headless/server setups where disk space matters more than local browsing. If an upload fails, the local file is always kept.

---

## Development

Run the unit test suite (no WebUI or network required — the WebUI modules and HTTP layer are mocked):

```bash
python3 -m pytest test_b2_storage.py test_b2_adapter.py
```

Live integration tests run against a real B2 bucket when the `B2_TEST_KEY_ID`, `B2_TEST_APPLICATION_KEY` (or `B2_TEST_APP_KEY`), `B2_TEST_BUCKET`, and optionally `B2_TEST_S3_ENDPOINT` (or `B2_TEST_ENDPOINT`) environment variables are set, and skip otherwise. They write only under an `integration-tests/` prefix and delete everything they create:

```bash
set -a; . ~/.api_keys; set +a   # or wherever your test credentials live
python3 -m pytest test_b2_live.py -v
```

* `b2_storage_adapter.py` — standalone B2 client (native REST + S3), reusable outside the WebUI.
* `scripts/b2_storage.py` — the WebUI extension: settings UI, save-hook, background upload queue.
* `install.py` — dependency bootstrap run by the WebUI on startup.

### Limitations

* Files larger than 200 MB should use B2's multipart (large file) API per the integration checklist. Generated images are far below this, so multipart upload is not implemented.
* To smoke-test resilience against a live bucket, B2 supports fault-injection headers such as `X-Bz-Test-Mode: fail_some_uploads` and `expire_some_account_authorization_tokens`.

---

## License

[MIT](LICENSE)
