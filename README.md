# Stable Diffusion WebUI Backblaze B2 Cloud Storage Extension

This extension automatically uploads all generated images from the [Stable Diffusion WebUI (AUTOMATIC1111)](https://github.com/AUTOMATIC1111/stable-diffusion-webui) to your **Backblaze B2 Cloud Storage** bucket, with options for native REST API, S3-Compatible API, and automatic local disk cleanup.

---

## Features
* **Auto-Upload:** Automatically intercepts generated images at the `on_image_saved` hook.
* **Dual Protocols:** Choose between B2's native REST API (highly lightweight, zero extra dependencies) or the standard S3-Compatible API (via `boto3`).
* **Auto-Cleanup:** Option to delete local copies of images immediately after successful upload, preventing your server or local machine from running out of disk space.
* **Folder Structure Preservation:** Uploads images preserving the relative directory path structure of the WebUI outputs (e.g. `outputs/txt2img-images/2026-07-09/00001.png`).

---

## Installation

1. Copy or clone this extension directory into the `extensions/` directory of your Stable Diffusion WebUI:
   ```bash
   stable-diffusion-webui/extensions/stable-diffusion-webui-b2-storage/
   ```
2. Restart the Stable Diffusion WebUI.

---

## Configuration

1. In the WebUI, navigate to the **Settings** tab.
2. Select **Backblaze B2 Storage** on the left menu.
3. Configure the settings:
   * **Enable Backblaze B2 Uploads:** Check to enable.
   * **B2 API Type:** Select `native` or `s3`.
   * **B2 Key ID / S3 Access Key:** Your Backblaze application Key ID.
   * **B2 Application Key / S3 Secret Key:** Your Backblaze application key.
   * **B2 Bucket Name:** Name of the B2 bucket.
   * **B2 S3 Endpoint:** Required only if using `s3` API type (e.g., `https://s3.us-west-004.backblazeb2.com`).
   * **Delete local image copy after successful B2 upload:** Check to delete the local file after it has been safely pushed to the cloud.
4. Click **Apply settings** at the top of the Settings page.
5. Generate an image! The progress logs will appear in your WebUI command terminal.
