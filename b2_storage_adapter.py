#!/usr/bin/env python3
"""
Backblaze B2 Storage Adapter Prototype
Supports both the Native B2 REST API and the S3-Compatible API.
"""

import os
import sys
import abc
import hashlib
import urllib.parse
import mimetypes
import logging
from typing import List, Dict, Any, Optional

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("B2Adapter")

# Try to import optional packages
try:
    import requests
except ImportError:
    logger.error("The 'requests' library is required for the Native B2 adapter. Install it with: pip install requests")
    sys.exit(1)

BOTO3_AVAILABLE = False
try:
    import boto3
    from botocore.exceptions import ClientError
    BOTO3_AVAILABLE = True
except ImportError:
    pass


class B2AdapterException(Exception):
    """Custom exception class for B2 Adapter errors."""
    pass


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
    """

    def __init__(self, key_id: str, application_key: str) -> None:
        self.key_id = key_id
        self.application_key = application_key
        self.account_id: Optional[str] = None
        self.api_url: Optional[str] = None
        self.download_url: Optional[str] = None
        self.auth_token: Optional[str] = None
        self.bucket_cache: Dict[str, str] = {}  # bucket_name -> bucket_id

    def authenticate(self) -> None:
        """Authorizes B2 account using b2_authorize_account API endpoint."""
        logger.info("Authenticating via B2 Native API...")
        auth_url = "https://api.backblazeb2.com/b2api/v3/b2_authorize_account"
        
        try:
            response = requests.get(
                auth_url,
                auth=(self.key_id, self.application_key),
                timeout=15
            )
            response.raise_for_status()
            data = response.json()
            
            self.account_id = data["accountId"]
            self.api_url = data["apiUrl"]
            self.download_url = data["downloadUrl"]
            self.auth_token = data["authorizationToken"]
            logger.info("Successfully authenticated. Account ID: %s", self.account_id)
        except Exception as e:
            raise B2AdapterException(f"Failed to authenticate via Native API: {e}")

    def _get_bucket_id(self, bucket_name: str) -> str:
        """Resolves bucket name to bucket ID using cache or api call."""
        if bucket_name in self.bucket_cache:
            return self.bucket_cache[bucket_name]

        if not self.api_url or not self.auth_token:
            self.authenticate()

        url = f"{self.api_url}/b2api/v3/b2_list_buckets"
        headers = {"Authorization": self.auth_token}
        payload = {"accountId": self.account_id}

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=15)
            response.raise_for_status()
            buckets = response.json().get("buckets", [])
            for bucket in buckets:
                self.bucket_cache[bucket["bucketName"]] = bucket["bucketId"]
            
            if bucket_name in self.bucket_cache:
                return self.bucket_cache[bucket_name]
            else:
                raise B2AdapterException(f"Bucket '{bucket_name}' not found.")
        except Exception as e:
            raise B2AdapterException(f"Failed to retrieve bucket list: {e}")

    def upload_file(self, local_path: str, remote_name: str, bucket_name: str) -> str:
        """Uploads a file using b2_get_upload_url and raw POST to B2."""
        if not os.path.exists(local_path):
            raise B2AdapterException(f"Local file does not exist: {local_path}")

        bucket_id = self._get_bucket_id(bucket_name)
        
        # Step 1: Get upload URL
        url = f"{self.api_url}/b2api/v3/b2_get_upload_url"
        headers = {"Authorization": self.auth_token}
        payload = {"bucketId": bucket_id}
        
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=15)
            response.raise_for_status()
            upload_data = response.json()
            upload_url = upload_data["uploadUrl"]
            upload_auth_token = upload_data["authorizationToken"]
        except Exception as e:
            raise B2AdapterException(f"Failed to get upload URL: {e}")

        # Step 2: Read file data and calculate SHA1
        logger.info("Uploading '%s' to B2 bucket '%s' as '%s'...", local_path, bucket_name, remote_name)
        with open(local_path, "rb") as f:
            file_data = f.read()
        
        sha1_hash = hashlib.sha1(file_data).hexdigest()
        content_type, _ = mimetypes.guess_type(local_path)
        if not content_type:
            content_type = "application/octet-stream"

        # Headers for B2 native upload
        upload_headers = {
            "Authorization": upload_auth_token,
            "X-Bz-File-Name": urllib.parse.quote(remote_name),
            "Content-Type": content_type,
            "Content-Length": str(len(file_data)),
            "X-Bz-Content-Sha1": sha1_hash
        }

        # Step 3: Perform upload POST
        try:
            response = requests.post(upload_url, headers=upload_headers, data=file_data, timeout=60)
            response.raise_for_status()
            upload_result = response.json()
            file_id = upload_result["fileId"]
            logger.info("Upload completed successfully. File ID: %s", file_id)
            return f"{self.download_url}/file/{bucket_name}/{remote_name}"
        except Exception as e:
            raise B2AdapterException(f"File upload failed: {e}")

    def download_file(self, remote_name: str, local_path: str, bucket_name: str) -> None:
        """Downloads file by name using direct HTTP GET."""
        if not self.download_url or not self.auth_token:
            self.authenticate()

        # Build download URL
        # Format: downloadUrl/file/bucketName/fileName
        encoded_name = "/".join(urllib.parse.quote(part) for part in remote_name.split("/"))
        url = f"{self.download_url}/file/{bucket_name}/{encoded_name}"
        headers = {"Authorization": self.auth_token}

        logger.info("Downloading '%s' from B2 bucket '%s' to '%s'...", remote_name, bucket_name, local_path)
        try:
            with requests.get(url, headers=headers, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)
            logger.info("Download completed successfully.")
        except Exception as e:
            raise B2AdapterException(f"File download failed: {e}")

    def list_files(self, bucket_name: str, prefix: str = "") -> List[Dict[str, Any]]:
        """Lists files using b2_list_file_names endpoint."""
        bucket_id = self._get_bucket_id(bucket_name)
        url = f"{self.api_url}/b2api/v3/b2_list_file_names"
        headers = {"Authorization": self.auth_token}
        payload = {
            "bucketId": bucket_id,
            "maxFileCount": 1000
        }
        if prefix:
            payload["prefix"] = prefix

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=15)
            response.raise_for_status()
            files_data = response.json().get("files", [])
            
            output = []
            for file in files_data:
                output.append({
                    "name": file["fileName"],
                    "size": file["contentLength"],
                    "id": file["fileId"],
                    "timestamp": file["uploadTimestamp"]
                })
            return output
        except Exception as e:
            raise B2AdapterException(f"Failed to list files: {e}")


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
        """Initialize S3 client with Backblaze B2 endpoint credentials."""
        logger.info("Connecting via B2 S3-Compatible API to endpoint %s...", self.endpoint_url)
        try:
            self.s3_client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.key_id,
                aws_secret_access_key=self.application_key
            )
            # Test authentication by listing buckets
            self.s3_client.list_buckets()
            logger.info("Successfully authenticated S3 API client.")
        except ClientError as e:
            raise B2AdapterException(f"S3 client authentication failed: {e}")

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
            return f"{self.endpoint_url}/{bucket_name}/{remote_name}"
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
        """Lists files using boto3 list_objects_v2."""
        if not self.s3_client:
            self.authenticate()

        try:
            params = {"Bucket": bucket_name}
            if prefix:
                params["Prefix"] = prefix

            response = self.s3_client.list_objects_v2(**params)
            objects = response.get("Contents", [])
            
            output = []
            for obj in objects:
                output.append({
                    "name": obj["Key"],
                    "size": obj["Size"],
                    "timestamp": obj["LastModified"].isoformat()
                })
            return output
        except ClientError as e:
            raise B2AdapterException(f"S3 list objects failed: {e}")
