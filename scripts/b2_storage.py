import os
import sys
import logging
import gradio as gr
from modules import shared, script_callbacks

# Add the parent extension directory to sys.path so we can import b2_storage_adapter
extension_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if extension_dir not in sys.path:
    sys.path.insert(0, extension_dir)

from b2_storage_adapter import B2NativeAdapter, B2S3Adapter, B2AdapterException

# Setup logger
logger = logging.getLogger("B2StorageExtension")
logger.setLevel(logging.INFO)

# Make sure logger logs to console
if not logger.handlers:
    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(logging.Formatter("[B2 Storage Extension] %(levelname)s: %(message)s"))
    logger.addHandler(ch)


def on_ui_settings():
    section = ('b2_storage', "Backblaze B2 Storage")
    
    # Register options in Settings tab
    shared.opts.add_option("b2_storage_enable", shared.OptionInfo(
        False, 
        "Enable Backblaze B2 Uploads", 
        section=section
    ))
    shared.opts.add_option("b2_storage_api_type", shared.OptionInfo(
        "native", 
        "B2 API Type", 
        gr.Radio, 
        {"choices": ["native", "s3"]}, 
        section=section
    ))
    shared.opts.add_option("b2_storage_key_id", shared.OptionInfo(
        "", 
        "B2 Key ID / S3 Access Key", 
        section=section
    ))
    shared.opts.add_option("b2_storage_application_key", shared.OptionInfo(
        "", 
        "B2 Application Key / S3 Secret Key", 
        section=section
    ))
    shared.opts.add_option("b2_storage_bucket", shared.OptionInfo(
        "", 
        "B2 Bucket Name", 
        section=section
    ))
    shared.opts.add_option("b2_storage_s3_endpoint", shared.OptionInfo(
        "", 
        "B2 S3 Endpoint (Required for S3 API type, e.g. https://s3.us-west-004.backblazeb2.com)", 
        section=section
    ))
    shared.opts.add_option("b2_storage_delete_local", shared.OptionInfo(
        False, 
        "Delete local image copy after successful B2 upload", 
        section=section
    ))


def on_image_saved(params: script_callbacks.ImageSaveParams):
    # Check if enabled
    enable = shared.opts.data.get("b2_storage_enable", False)
    if not enable:
        return

    # Extract settings
    api_type = shared.opts.data.get("b2_storage_api_type", "native")
    key_id = shared.opts.data.get("b2_storage_key_id", "")
    application_key = shared.opts.data.get("b2_storage_application_key", "")
    bucket = shared.opts.data.get("b2_storage_bucket", "")
    s3_endpoint = shared.opts.data.get("b2_storage_s3_endpoint", "")
    delete_local = shared.opts.data.get("b2_storage_delete_local", False)

    # Validate configurations
    if not key_id or not application_key or not bucket:
        logger.warning("Upload enabled but missing Key ID, Application Key, or Bucket Name in Settings. Skipping B2 upload.")
        return

    local_path = params.filename
    if not local_path or not os.path.exists(local_path):
        logger.warning(f"Local file does not exist: {local_path}. Skipping B2 upload.")
        return

    # Determine remote name based on filename relative to SD WebUI main directory
    # e.g., "outputs/txt2img-images/2026-07-09/00001.png"
    base_dir = os.path.abspath(os.path.join(extension_dir, "..", ".."))
    abs_local_path = os.path.abspath(local_path)
    if abs_local_path.startswith(base_dir):
        remote_name = os.path.relpath(abs_local_path, base_dir)
    else:
        remote_name = os.path.basename(local_path)

    # Normalize path separators for cloud storage (always forward slashes)
    remote_name = remote_name.replace("\\", "/")

    logger.info(f"Initializing B2 upload for '{remote_name}' using API type '{api_type}'...")

    try:
        if api_type == "s3":
            if not s3_endpoint:
                logger.error("S3 API type selected but B2 S3 Endpoint is not configured. Skipping upload.")
                return
            adapter = B2S3Adapter(key_id, application_key, s3_endpoint)
        else:
            adapter = B2NativeAdapter(key_id, application_key)

        # Authenticate and Upload
        adapter.authenticate()
        url = adapter.upload_file(abs_local_path, remote_name, bucket)
        logger.info(f"Upload completed successfully. B2 Remote URL: {url}")

        # Delete local copy if enabled
        if delete_local:
            try:
                os.remove(abs_local_path)
                logger.info(f"Deleted local image copy to save space: {abs_local_path}")
            except Exception as delete_err:
                logger.error(f"Failed to delete local file: {delete_err}")

    except B2AdapterException as b2_err:
        logger.error(f"Backblaze B2 upload failed: {b2_err}")
    except Exception as e:
        logger.error(f"An unexpected error occurred during B2 upload: {e}")


# Register callbacks in AUTOMATIC1111 script pipeline
script_callbacks.on_ui_settings(on_ui_settings)
script_callbacks.on_image_saved(on_image_saved)
