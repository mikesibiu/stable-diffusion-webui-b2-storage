import os
import sys
import queue
import logging
import threading
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

# Uploads run on a background worker so image generation is never blocked
# by network I/O. The adapter (and its 24h auth token) is cached between
# uploads and rebuilt only when credentials/settings change or a job fails.
_upload_queue: "queue.Queue" = queue.Queue()
_worker_thread = None
_worker_lock = threading.Lock()
_adapter_cache = {}

# Settings left empty in the UI fall back to these environment variables
# (same names the official Backblaze CLI uses, where applicable).
ENV_FALLBACKS = {
    "b2_storage_key_id": "B2_APPLICATION_KEY_ID",
    "b2_storage_application_key": "B2_APPLICATION_KEY",
    "b2_storage_bucket": "B2_BUCKET",
    "b2_storage_s3_endpoint": "B2_S3_ENDPOINT",
}


def _get_setting(name: str, default=""):
    value = shared.opts.data.get(name, default)
    if not value and name in ENV_FALLBACKS:
        value = os.environ.get(ENV_FALLBACKS[name], default)
    return value


def on_ui_settings():
    section = ('b2_storage', "Backblaze B2 Storage")

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
        "B2 Key ID / S3 Access Key (falls back to B2_APPLICATION_KEY_ID env var)",
        section=section
    ))
    shared.opts.add_option("b2_storage_application_key", shared.OptionInfo(
        "",
        "B2 Application Key / S3 Secret Key (falls back to B2_APPLICATION_KEY env var)",
        gr.Textbox,
        {"type": "password"},
        section=section
    ))
    shared.opts.add_option("b2_storage_bucket", shared.OptionInfo(
        "",
        "B2 Bucket Name (falls back to B2_BUCKET env var)",
        section=section
    ))
    shared.opts.add_option("b2_storage_s3_endpoint", shared.OptionInfo(
        "",
        "B2 S3 Endpoint (Required for S3 API type, e.g. https://s3.us-west-004.backblazeb2.com)",
        section=section
    ))
    shared.opts.add_option("b2_storage_delete_local", shared.OptionInfo(
        False,
        "Delete local image copy after successful B2 upload (see README caveats)",
        section=section
    ))


def on_image_saved(params: script_callbacks.ImageSaveParams):
    """Validate settings and queue the saved image for background upload."""
    if not shared.opts.data.get("b2_storage_enable", False):
        return

    api_type = shared.opts.data.get("b2_storage_api_type", "native")
    key_id = _get_setting("b2_storage_key_id")
    application_key = _get_setting("b2_storage_application_key")
    bucket = _get_setting("b2_storage_bucket")
    s3_endpoint = _get_setting("b2_storage_s3_endpoint")
    delete_local = shared.opts.data.get("b2_storage_delete_local", False)

    if not key_id or not application_key or not bucket:
        logger.warning("Upload enabled but missing Key ID, Application Key, or Bucket Name "
                       "in Settings (or env vars). Skipping B2 upload.")
        return

    if api_type == "s3" and not s3_endpoint:
        logger.error("S3 API type selected but B2 S3 Endpoint is not configured. Skipping upload.")
        return

    local_path = params.filename
    if not local_path or not os.path.exists(local_path):
        logger.warning(f"Local file does not exist: {local_path}. Skipping B2 upload.")
        return

    # Determine remote name based on filename relative to the WebUI root
    # e.g., "outputs/txt2img-images/2026-07-09/00001.png"
    base_dir = os.path.abspath(os.path.join(extension_dir, "..", ".."))
    abs_local_path = os.path.abspath(local_path)
    if abs_local_path.startswith(base_dir + os.sep):
        remote_name = os.path.relpath(abs_local_path, base_dir)
    else:
        remote_name = os.path.basename(local_path)

    # Normalize path separators for cloud storage (always forward slashes)
    remote_name = remote_name.replace("\\", "/")

    _upload_queue.put({
        "api_type": api_type,
        "key_id": key_id,
        "application_key": application_key,
        "s3_endpoint": s3_endpoint,
        "bucket": bucket,
        "local_path": abs_local_path,
        "remote_name": remote_name,
        "delete_local": delete_local,
    })
    _ensure_worker()
    logger.info(f"Queued B2 upload for '{remote_name}' using API type '{api_type}'.")


def _ensure_worker():
    """Start the background upload thread if it isn't already running."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(
                target=_worker_loop, name="b2-storage-upload", daemon=True
            )
            _worker_thread.start()


def _worker_loop():
    while True:
        job = _upload_queue.get()
        try:
            _process_job(job)
        finally:
            _upload_queue.task_done()


def _get_adapter(job):
    """Return a cached, authenticated adapter for the job's settings."""
    cache_key = (job["api_type"], job["key_id"], job["application_key"], job["s3_endpoint"])
    adapter = _adapter_cache.get(cache_key)
    if adapter is None:
        if job["api_type"] == "s3":
            adapter = B2S3Adapter(job["key_id"], job["application_key"], job["s3_endpoint"])
        else:
            adapter = B2NativeAdapter(job["key_id"], job["application_key"])
        adapter.authenticate()
        _adapter_cache.clear()  # settings changed; drop adapters for old settings
        _adapter_cache[cache_key] = adapter
    return adapter


def _process_job(job):
    """Upload one image to B2; never raises so the worker keeps running."""
    try:
        adapter = _get_adapter(job)
        url = adapter.upload_file(job["local_path"], job["remote_name"], job["bucket"])
        logger.info(f"Upload completed successfully. B2 Remote URL: {url}")

        if job["delete_local"]:
            try:
                os.remove(job["local_path"])
                logger.info(f"Deleted local image copy to save space: {job['local_path']}")
            except Exception as delete_err:
                logger.error(f"Failed to delete local file: {delete_err}")

    except B2AdapterException as b2_err:
        logger.error(f"Backblaze B2 upload failed: {b2_err}")
        _adapter_cache.clear()  # force a fresh authentication on the next upload
    except Exception as e:
        logger.error(f"An unexpected error occurred during B2 upload: {e}")
        _adapter_cache.clear()


# Register callbacks in AUTOMATIC1111 script pipeline
script_callbacks.on_ui_settings(on_ui_settings)
script_callbacks.on_image_saved(on_image_saved)
