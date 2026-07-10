"""
Dependency installer, run automatically by AUTOMATIC1111 WebUI on startup.

'requests' ships with the WebUI, but install it defensively for forks that
slim their environment. 'boto3' is only needed for the optional S3 API type,
so it is not installed by default — see README.
"""
import launch

if not launch.is_installed("requests"):
    launch.run_pip("install requests", "requests for B2 Storage extension")
