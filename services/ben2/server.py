"""BEN2 background-removal HTTP service.

Loads the PramaLLC/BEN2 model once, then serves removal requests over HTTP.
Accepts image paths (server-local) and returns the foreground RGBA image as
base64-encoded PNG.

Usage:
    python -m services.ben2.server --port 8003
"""

import argparse
import base64
import io
import os
from typing import Dict, Optional

import torch
import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from PIL import Image

from ben2 import BEN_Base

import logging

logger = logging.getLogger(__name__)


class RemoveRequest(BaseModel):
    image_path: str = Field(..., description="Absolute path to the image file")
    output_name: Optional[str] = Field(None, description="Optional stem for output filename")


class RemoveResponse(BaseModel):
    image_base64: str = Field(..., description="Foreground RGBA image as base64-encoded PNG")
    width: int
    height: int


class BEN2Runtime:
    def __init__(self, model_path: Optional[str] = None, device: str = "cuda") -> None:
        model_id = model_path if (model_path and os.path.exists(model_path)) else "PramaLLC/BEN2"
        logger.info("Loading model from %s on %s ...", model_id, device)
        self.device = torch.device(device)
        self.model = BEN_Base.from_pretrained(model_id).to(self.device).eval()
        logger.info("Model loaded on %s", self.device)

    def remove_background(self, image: Image.Image) -> Image.Image:
        return self.model.inference(image)


def create_app(runtime: BEN2Runtime) -> FastAPI:
    app = FastAPI(title="BEN2 Background Removal Service", version="1.0.0")

    @app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/remove", response_model=RemoveResponse)
    async def remove(request: RemoveRequest) -> RemoveResponse:
        if not os.path.exists(request.image_path):
            raise HTTPException(status_code=404, detail=f"Image not found: {request.image_path}")
        try:
            image = Image.open(request.image_path).convert("RGB")
            foreground = runtime.remove_background(image)

            buf = io.BytesIO()
            foreground.save(buf, format="PNG")
            buf.seek(0)
            b64 = base64.b64encode(buf.read()).decode("ascii")

            return RemoveResponse(
                image_base64=b64,
                width=foreground.width,
                height=foreground.height,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/remove_region")
    async def remove_region(request: dict) -> Dict:
        """Remove background from a cropped region of an image.

        Accepts: {"image_path": str, "x1": int, "y1": int, "x2": int, "y2": int}
        """
        image_path = request.get("image_path", "")
        if not os.path.exists(image_path):
            raise HTTPException(status_code=404, detail=f"Image not found: {image_path}")
        try:
            image = Image.open(image_path).convert("RGB")
            x1, y1 = request.get("x1", 0), request.get("y1", 0)
            x2, y2 = request.get("x2", image.width), request.get("y2", image.height)
            cropped = image.crop((x1, y1, x2, y2))

            foreground = runtime.remove_background(cropped)

            buf = io.BytesIO()
            foreground.save(buf, format="PNG")
            buf.seek(0)
            b64 = base64.b64encode(buf.read()).decode("ascii")

            return {
                "image_base64": b64,
                "width": foreground.width,
                "height": foreground.height,
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Start the BEN2 background removal service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", type=int, default=8003, help="Port to bind")
    parser.add_argument("--model-path", default=None, help="Local model path (default: download from HF)")
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    args = parser.parse_args()

    runtime = BEN2Runtime(model_path=args.model_path, device=args.device)
    uvicorn.run(create_app(runtime), host=args.host, port=args.port, workers=1)
