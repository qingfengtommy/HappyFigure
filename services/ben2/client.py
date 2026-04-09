"""HTTP client for the BEN2 background-removal service."""
import base64
import io
from typing import Optional

import requests
from PIL import Image


class BEN2ServiceClient:
    def __init__(self, base_url: str, timeout: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> bool:
        resp = requests.get(f"{self.base_url}/health", timeout=5)
        return resp.status_code == 200

    def remove_background(self, image_path: str) -> Image.Image:
        """Send an image path and get back an RGBA PIL Image."""
        resp = requests.post(
            f"{self.base_url}/remove",
            json={"image_path": image_path},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        img_bytes = base64.b64decode(data["image_base64"])
        return Image.open(io.BytesIO(img_bytes)).convert("RGBA")

    def remove_background_region(
        self, image_path: str, x1: int, y1: int, x2: int, y2: int,
    ) -> Image.Image:
        """Remove background from a cropped region."""
        resp = requests.post(
            f"{self.base_url}/remove_region",
            json={"image_path": image_path, "x1": x1, "y1": y1, "x2": x2, "y2": y2},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        img_bytes = base64.b64decode(data["image_base64"])
        return Image.open(io.BytesIO(img_bytes)).convert("RGBA")


def check_health(endpoints: Optional[list] = None, timeout: int = 5) -> bool:
    """Return True if the BEN2 service is reachable."""
    endpoints = endpoints or ["http://127.0.0.1:8003"]
    for url in endpoints:
        try:
            client = BEN2ServiceClient(url, timeout=timeout)
            if client.health():
                return True
        except (requests.RequestException, OSError):
            pass
    return False
