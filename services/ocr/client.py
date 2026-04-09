"""HTTP client for the PaddleOCR service."""
import itertools
import threading
from typing import Dict, List, Optional

import requests


class OcrServiceClient:
    def __init__(self, base_url: str, timeout: int = 120) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def health(self) -> bool:
        resp = requests.get(f"{self.base_url}/health", timeout=5)
        return resp.status_code == 200

    def predict(
        self,
        image_path: str,
        regions: Optional[List[Dict]] = None,
    ) -> Dict:
        payload: Dict = {"image_path": image_path}
        if regions is not None:
            payload["regions"] = regions
        resp = requests.post(
            f"{self.base_url}/predict", json=payload, timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()


class OcrServicePool:
    def __init__(self, endpoints: List[str], timeout: int = 120) -> None:
        if len(endpoints) == 0:
            raise ValueError("At least one endpoint is required")
        self.clients = [OcrServiceClient(url, timeout=timeout) for url in endpoints]
        self._lock = threading.Lock()
        self._cursor = itertools.cycle(range(len(self.clients)))

    def predict(self, *args, **kwargs) -> Dict:
        with self._lock:
            idx = next(self._cursor)
        return self.clients[idx].predict(*args, **kwargs)

    def health(self) -> Dict[str, bool]:
        status: Dict[str, bool] = {}
        for client in self.clients:
            try:
                status[client.base_url] = client.health()
            except Exception:
                status[client.base_url] = False
        return status


def check_health(endpoints=None, timeout=5):
    """Return True if OCR service is reachable."""
    endpoints = endpoints or ["http://127.0.0.1:8002"]
    try:
        pool = OcrServicePool(endpoints, timeout=timeout)
        return any(pool.health().values())
    except Exception:
        return False
