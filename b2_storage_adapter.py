#!/usr/bin/env python3
"""
Backblaze B2 Storage Adapter
Supports both the Native B2 REST API and the S3-Compatible API.
"""

import os
import abc
import time
import hashlib
import platform
import urllib.parse
import mimetypes
import logging
from typing import List, Dict, Any, Optional, Tuple

logger = logging.getLogger("B2Adapter")
logger.addHandler(logging.NullHandler())

try:
    import requests
except ImportError as e:
    raise ImportError(
        "The 'requests' library is required for the B2 storage adapter. "
        "Install it with: pip install requests"
    ) from e

BOTO3_AVAILABLE = False
try:
    import boto3
    from botocore.exceptions import ClientError
    from botocore.config import Config as BotoConfig
    BOTO3_AVAILABLE = True
except ImportError:
    pass

__version__ = "1.0.0"

# B2 integration checklist: identify the integration on every API call.
USER_AGENT = f"sd-webui-b2-storage/{__version__}+python/{platform.python_version()}"

# B2 protocol: on a failed upload, request a fresh upload URL and retry.
MAX_UPLOAD_ATTEMPTS = 3
API_TIMEOUT = 15
TRANSFER_TIMEOUT = 60
# B2 service limit for single-part uploads; beyond this the large-file
# (multipart) API is required, which this adapter does not implement.
MAX_SINGLE_UPLOAD_BYTES = 5 * 1024 ** 3


class B2AdapterException(Exception):
    """Custom exception class for B2 Adapter errors."""
    pass


def _encode_file_name(name: str) -> str:
    """Percent-encode a B2 file name, keeping '/' separators intact."""
    return urllib.parse.quote(name, safe="/")


def _retry_delay(attempt: int, retry_after: Optional[str]) -> float:
    """Checklist: honor Retry-After when present, else exponential backoff from 1s."""
    if retry_after:
        try:
            return float(retry_after)
        except ValueError:
            pass
    return float(2 ** (attempt - 1))


class B2StorageAdapter(abc.ABC):
    """
    Abstract Base Class for Backblaze B2 storage adapters.
    Provides a unified interface regardless of the underlying protocol.
    """

    @abc.abstractmethod
    def authenticate(self) -> None:
        """Authenticate with the Backblaze B2 service."""
        pass

    @abc.abstractmethod
    def upload_file(self, local_path: str, remote_name: str, bucket_name: str) -> str:
        """
        Upload a file to a B2 bucket.

        Args:
            local_path: Absolute or relative path to the local file.
            remote_name: Destination path/name of the file in the bucket.
            bucket_name: Name of the bucket.

        Returns:
            The URL or identifier of the uploaded file.
        """
        pass

    @abc.abstractmethod
    def download_file(self, remote_name: str, local_path: str, bucket_name: str) -> None:
        """
        Download a file from a B2 bucket.

        Args:
            remote_name: Path/name of the file in the bucket.
            local_path: Destination path on the local disk.
            bucket_name: Name of the bucket.
        """
        pass

    @abc.abstractmethod
    def list_files(self, bucket_name: str, prefix: str = "") -> List[Dict[str, Any]]:
        """
        List files in a B2 bucket.

        Args:
            bucket_name: Name of the bucket.
            prefix: Filter results by this prefix.

        Returns:
            A list of dictionaries representing the files.
        """
        pass


class B2NativeAdapter(B2StorageAdapter):
    """
    Backblaze B2 Native API Client.
    Uses the B2 REST API directly over HTTPS with the requests library.

    Handles both account-wide and single-bucket-restricted application keys,
    and transparently re-authenticates when the auth token expires.
    """

    def __init__(self, key_id: str, application_key: str) -> None:
        self.key_id = key_id
        self.application_key = application_key
        self.account_id: Optional[str] = None
        self.api_url: Optional[str] = None
        self.download_url: Optional[str] = None
        self.auth_token: Optional[str] = None
        self.allowed_bucket_id: Optional[str] = None
        self.allowed_bucket_name: Optional[str] = None
        self.bucket_cache: Dict[str, str] = {}  # bucket_name -> bucket_id
        # Checklist: an upload URL/token pair may be reused for many files
        # until an upload fails. bucket_id -> (upload_url, upload_auth_token)
        self._upload_url_cache: Dict[str, Tuple[str, str]] = {}

    def authenticate(self) -> None:
        """Authorizes B2 account using the b2_authorize_account API endpoint."""
        logger.info("Authenticating via B2 Native API...")
        auth_url = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"

        try:
            response = requests.get(
                auth_url,
                auth=(self.key_id, self.application_key),
                headers={"User-Agent": USER_AGENT},
                timeout=API_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()

            # v3 nests URLs and key restrictions in apiInfo.storageApi;
            # v2 kept them top-level (apiUrl, downloadUrl, allowed).
            storage = data.get("apiInfo", {}).get("storageApi", {})
            allowed = data.get("allowed", {})

            self.account_id = data["accountId"]
            self.auth_token = data["authorizationToken"]
            self.api_url = storage.get("apiUrl") or data.get("apiUrl")
            self.download_url = storage.get("downloadUrl") or data.get("downloadUrl")
            self.allowed_bucket_id = storage.get("bucketId") or allowed.get("bucketId")
            self.allowed_bucket_name = storage.get("bucketName") or allowed.get("bucketName")

            if not self.api_url or not self.download_url:
                raise B2AdapterException("Authorization response missing apiUrl/downloadUrl.")
            logger.info("Successfully authenticated. Account ID: %s", self.account_id)
        except B2AdapterException:
            raise
        except Exception as e:
            raise B2AdapterException(f"Failed to authenticate via Native API: {e}")

    def _api_request(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """
        POST to a B2 API endpoint. Re-authenticates once and retries if the
        auth token has expired (tokens are valid for 24 hours).
        """
        for attempt in range(2):
            if not self.auth_token:
                self.authenticate()
            url = f"{self.api_url}/b2api/v3/{endpoint}"
            response = requests.post(
                url,
                headers={"Authorization": self.auth_token, "User-Agent": USER_AGENT},
                json=payload,
                timeout=API_TIMEOUT
            )
            if response.status_code == 401 and attempt == 0:
                logger.info("Auth token rejected; re-authenticating...")
                self.auth_token = None
                continue
            response.raise_for_status()
            return response.json()
        raise B2AdapterException(f"Request to {endpoint} failed after re-authentication.")

    def _get_bucket_id(self, bucket_name: str) -> str:
        """Resolves bucket name to bucket ID using the key's own bucket restriction, cache, or an API call."""
        if bucket_name in self.bucket_cache:
            return self.bucket_cache[bucket_name]

        if not self.auth_token:
            self.authenticate()

        # Keys restricted to a single bucket cannot list buckets account-wide,
        # but the authorization response already tells us their bucket.
        if self.allowed_bucket_id:
            if bucket_name == self.allowed_bucket_name:
                self.bucket_cache[bucket_name] = self.allowed_bucket_id
                return self.allowed_bucket_id
            raise B2AdapterException(
                f"This application key is restricted to bucket '{self.allowed_bucket_name}' "
                f"and cannot access '{bucket_name}'."
            )

        try:
            data = self._api_request("b2_list_buckets", {"accountId": self.account_id})
        except B2AdapterException:
            raise
        except Exception as e:
            raise B2AdapterException(f"Failed to retrieve bucket list: {e}")

        for bucket in data.get("buckets", []):
            self.bucket_cache[bucket["bucketName"]] = bucket["bucketId"]

        if bucket_name in self.bucket_cache:
            return self.bucket_cache[bucket_name]
        raise B2AdapterException(f"Bucket '{bucket_name}' not found.")

    def _get_upload_target(self, bucket_id: str) -> Tuple[str, str]:
        """Return a cached (upload_url, upload_auth_token) pair, or request a new one."""
        cached = self._upload_url_cache.get(bucket_id)
        if cached:
            return cached
        data = self._api_request("b2_get_upload_url", {"bucketId": bucket_id})
        target = (data["uploadUrl"], data["authorizationToken"])
        self._upload_url_cache[bucket_id] = target
        return target

    def upload_file(self, local_path: str, remote_name: str, bucket_name: str) -> str:
        """
        Uploads a file via b2_get_upload_url + POST, following the B2
        integration checklist:
        - the upload URL/token is reused across files until an upload fails;
        - connection failures, 401/408/429 and 5xx responses are retried with
          a freshly requested upload URL, honoring Retry-After / exponential
          backoff, up to MAX_UPLOAD_ATTEMPTS times;
        - other 4xx responses (e.g. 403 account cap) fail immediately.
        """
        if not os.path.exists(local_path):
            raise B2AdapterException(f"Local file does not exist: {local_path}")

        file_size = os.path.getsize(local_path)
        if file_size > MAX_SINGLE_UPLOAD_BYTES:
            raise B2AdapterException(
                f"File is {file_size} bytes; single-part B2 uploads are capped at "
                f"{MAX_SINGLE_UPLOAD_BYTES} bytes and the large-file API is not implemented."
            )

        bucket_id = self._get_bucket_id(bucket_name)

        # Two-pass streaming: hash in chunks first, then stream the body,
        # so the file is never held fully in memory.
        sha1 = hashlib.sha1()
        with open(local_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha1.update(chunk)
        sha1_hash = sha1.hexdigest()

        src_mtime_millis = str(int(os.path.getmtime(local_path) * 1000))
        content_type, _ = mimetypes.guess_type(local_path)
        if not content_type:
            content_type = "application/octet-stream"

        logger.info("Uploading '%s' to B2 bucket '%s' as '%s'...", local_path, bucket_name, remote_name)

        last_error: Optional[BaseException] = None
        for attempt in range(1, MAX_UPLOAD_ATTEMPTS + 1):
            retry_after = None
            try:
                upload_url, upload_token = self._get_upload_target(bucket_id)
                upload_headers = {
                    "Authorization": upload_token,
                    "User-Agent": USER_AGENT,
                    "X-Bz-File-Name": _encode_file_name(remote_name),
                    "Content-Type": content_type,
                    "Content-Length": str(file_size),
                    "X-Bz-Content-Sha1": sha1_hash,
                    "X-Bz-Info-src_last_modified_millis": src_mtime_millis
                }
                # Fresh handle per attempt: a retried upload must restart the stream
                with open(local_path, "rb") as body:
                    response = requests.post(
                        upload_url,
                        headers=upload_headers,
                        data=body,
                        timeout=TRANSFER_TIMEOUT
                    )
                status = response.status_code
                if status == 200:
                    file_id = response.json()["fileId"]
                    logger.info("Upload completed successfully. File ID: %s", file_id)
                    return f"{self.download_url}/file/{bucket_name}/{_encode_file_name(remote_name)}"

                # Any failure invalidates the upload URL for future attempts/files.
                self._upload_url_cache.pop(bucket_id, None)
                if status == 403:
                    raise B2AdapterException(
                        "Upload forbidden (403): check your Backblaze account caps and alerts."
                    )
                if status not in (401, 408, 429) and not 500 <= status < 600:
                    raise B2AdapterException(f"Upload failed with HTTP {status}.")
                retry_after = response.headers.get("Retry-After")
                last_error = Exception(f"HTTP {status}")
            except B2AdapterException:
                raise  # config/bucket/cap errors are not retryable
            except Exception as e:
                # Connection-level failure (timeout, reset, broken pipe) — retryable.
                self._upload_url_cache.pop(bucket_id, None)
                last_error = e

            if attempt < MAX_UPLOAD_ATTEMPTS:
                delay = _retry_delay(attempt, retry_after)
                logger.warning(
                    "Upload attempt %d/%d failed (%s); retrying in %.1fs with a new upload URL...",
                    attempt, MAX_UPLOAD_ATTEMPTS, last_error, delay
                )
                time.sleep(delay)

        raise B2AdapterException(
            f"File upload failed after {MAX_UPLOAD_ATTEMPTS} attempts: {last_error}"
        )

    def download_file(self, remote_name: str, local_path: str, bucket_name: str) -> None:
        """Downloads file by name using direct HTTP GET."""
        if not self.download_url or not self.auth_token:
            self.authenticate()

        # Format: downloadUrl/file/bucketName/fileName
        url = f"{self.download_url}/file/{bucket_name}/{_encode_file_name(remote_name)}"
        headers = {"Authorization": self.auth_token}

        logger.info("Downloading '%s' from B2 bucket '%s' to '%s'...", remote_name, bucket_name, local_path)
        try:
            with requests.get(url, headers=headers, stream=True, timeout=TRANSFER_TIMEOUT) as r:
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            logger.info("Download completed successfully.")
        except Exception as e:
            raise B2AdapterException(f"File download failed: {e}")

    def list_files(self, bucket_name: str, prefix: str = "") -> List[Dict[str, Any]]:
        """Lists all files using b2_list_file_names, following pagination."""
        bucket_id = self._get_bucket_id(bucket_name)
        payload: Dict[str, Any] = {
            "bucketId": bucket_id,
            "maxFileCount": 1000
        }
        if prefix:
            payload["prefix"] = prefix

        output = []
        try:
            while True:
                data = self._api_request("b2_list_file_names", payload)
                for file in data.get("files", []):
                    output.append({
                        "name": file["fileName"],
                        "size": file["contentLength"],
                        "id": file["fileId"],
                        "timestamp": file["uploadTimestamp"]
                    })
                next_name = data.get("nextFileName")
                if not next_name:
                    break
                payload["startFileName"] = next_name
        except B2AdapterException:
            raise
        except Exception as e:
            raise B2AdapterException(f"Failed to list files: {e}")
        return output


class B2S3Adapter(B2StorageAdapter):
    """
    Backblaze B2 S3-Compatible API Client.
    Uses boto3 configured with a Backblaze custom endpoint.
    """

    def __init__(self, key_id: str, application_key: str, endpoint_url: str) -> None:
        """
        Args:
            key_id: B2 Application Key ID.
            application_key: B2 Application Key.
            endpoint_url: B2 S3 endpoint, e.g., 'https://s3.us-west-004.backblazeb2.com'
        """
        if not BOTO3_AVAILABLE:
            raise B2AdapterException("The 'boto3' library is not installed. Run: pip install boto3")
        self.key_id = key_id
        self.application_key = application_key
        self.endpoint_url = endpoint_url
        self.s3_client = None

    def authenticate(self) -> None:
        """
        Initialize the S3 client with Backblaze B2 endpoint credentials.
        Credentials are validated lazily on the first request, since keys
        restricted to a single bucket are not allowed to list buckets.
        """
        logger.info("Connecting via B2 S3-Compatible API to endpoint %s...", self.endpoint_url)
        try:
            self.s3_client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.key_id,
                aws_secret_access_key=self.application_key,
                config=BotoConfig(
                    user_agent_extra=USER_AGENT,
                    retries={"max_attempts": MAX_UPLOAD_ATTEMPTS, "mode": "standard"}
                )
            )
            logger.info("S3 API client initialized.")
        except Exception as e:
            raise B2AdapterException(f"S3 client initialization failed: {e}")

    def upload_file(self, local_path: str, remote_name: str, bucket_name: str) -> str:
        """Uploads file using boto3 client upload_file."""
        if not self.s3_client:
            self.authenticate()

        logger.info("Uploading '%s' to B2 bucket '%s' via S3 API...", local_path, bucket_name)
        try:
            content_type, _ = mimetypes.guess_type(local_path)
            extra_args = {}
            if content_type:
                extra_args["ContentType"] = content_type

            self.s3_client.upload_file(
                Filename=local_path,
                Bucket=bucket_name,
                Key=remote_name,
                ExtraArgs=extra_args
            )
            logger.info("Upload completed via S3.")
            return f"{self.endpoint_url}/{bucket_name}/{_encode_file_name(remote_name)}"
        except ClientError as e:
            raise B2AdapterException(f"S3 upload failed: {e}")

    def download_file(self, remote_name: str, local_path: str, bucket_name: str) -> None:
        """Downloads file using boto3 client download_file."""
        if not self.s3_client:
            self.authenticate()

        logger.info("Downloading '%s' from B2 bucket '%s' to '%s' via S3 API...", remote_name, bucket_name, local_path)
        try:
            self.s3_client.download_file(
                Bucket=bucket_name,
                Key=remote_name,
                Filename=local_path
            )
            logger.info("Download completed via S3.")
        except ClientError as e:
            raise B2AdapterException(f"S3 download failed: {e}")

    def list_files(self, bucket_name: str, prefix: str = "") -> List[Dict[str, Any]]:
        """Lists all files using the list_objects_v2 paginator."""
        if not self.s3_client:
            self.authenticate()

        try:
            params = {"Bucket": bucket_name}
            if prefix:
                params["Prefix"] = prefix

            output = []
            paginator = self.s3_client.get_paginator("list_objects_v2")
            for page in paginator.paginate(**params):
                for obj in page.get("Contents", []):
                    output.append({
                        "name": obj["Key"],
                        "size": obj["Size"],
                        "timestamp": obj["LastModified"].isoformat()
                    })
            return output
        except ClientError as e:
            raise B2AdapterException(f"S3 list objects failed: {e}")
