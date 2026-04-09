"""PPStructureV3 OCR HTTP service.

Loads PPStructureV3 (PaddlePaddle's latest document structure + OCR engine)
once and serves OCR requests over HTTP.  PPStructureV3 uses PP-OCRv5 server
models for higher-accuracy text detection and recognition.

Usage:
    python -m services.ocr.server --host 0.0.0.0 --port 8002
"""
import argparse
import asyncio
import os
import time
from typing import Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from PIL import Image
from pydantic import BaseModel, Field

# Suppress paddlex connectivity check
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

import logging

logger = logging.getLogger(__name__)


# ---- Request / Response schemas ----

class OcrRequest(BaseModel):
    image_path: str = Field(..., description="Path to image file the server can read")
    regions: Optional[List[Dict]] = Field(
        None,
        description=(
            "Optional list of crop regions: [{x, y, width, height, id, padding}]. "
            "If omitted the full image is processed."
        ),
    )


class OcrTextItem(BaseModel):
    text: str
    score: float
    poly: List[List[int]]  # [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]


class OcrRegionResult(BaseModel):
    id: str
    text: str
    items: List[OcrTextItem] = []


class OcrResponse(BaseModel):
    image_size: Dict[str, int]
    full_text: str = ""
    items: List[OcrTextItem] = []
    regions: List[OcrRegionResult] = []


# ---- Runtime ----

class OcrRuntime:
    def __init__(self, lang: str = "en") -> None:
        from paddleocr import PPStructureV3

        logger.info("Loading PPStructureV3 (lang=%s) ...", lang)
        t0 = time.time()
        self.ocr = PPStructureV3(
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_table_recognition=False,
            use_formula_recognition=False,
            use_chart_recognition=False,
            use_seal_recognition=False,
            use_region_detection=False,
        )
        elapsed = time.time() - t0
        self.inference_lock = asyncio.Lock()
        logger.info("PPStructureV3 loaded (%.1fs).", elapsed)

    def _run_ocr_on_image(self, img_path_or_array) -> tuple[str, list[OcrTextItem]]:
        """Run PPStructureV3 and return (full_text, items)."""
        results = list(self.ocr.predict(input=img_path_or_array))
        if not results:
            return "", []

        res = results[0]

        # PPStructureV3 nests OCR results in overall_ocr_res
        ocr_res = res.get("overall_ocr_res", {})
        if not ocr_res:
            return "", []

        texts = ocr_res.get("rec_texts", [])
        scores = ocr_res.get("rec_scores", [])
        polys = ocr_res.get("rec_polys", [])

        items: list[OcrTextItem] = []
        for text, score, poly in zip(texts, scores, polys):
            score_val = float(score) if hasattr(score, 'item') else float(score)
            poly_list = poly.tolist() if hasattr(poly, "tolist") else poly
            items.append(OcrTextItem(text=text, score=score_val, poly=poly_list))

        full_text = "\n".join(texts)
        return full_text, items

    async def predict(self, payload: OcrRequest) -> OcrResponse:
        async with self.inference_lock:
            if not os.path.exists(payload.image_path):
                raise FileNotFoundError(f"Image not found: {payload.image_path}")

            img = Image.open(payload.image_path).convert("RGB")
            img_w, img_h = img.size

            # Full-image mode
            if not payload.regions:
                full_text, items = self._run_ocr_on_image(payload.image_path)
                return OcrResponse(
                    image_size={"width": img_w, "height": img_h},
                    full_text=full_text,
                    items=items,
                )

            # Region mode — crop each region and run OCR
            import numpy as np

            region_results: list[OcrRegionResult] = []
            for region in payload.regions:
                padding = region.get("padding", 5)
                x1 = max(0, int(region["x"]) - padding)
                y1 = max(0, int(region["y"]) - padding)
                x2 = min(img_w, int(region["x"]) + int(region["width"]) + padding)
                y2 = min(img_h, int(region["y"]) + int(region["height"]) + padding)
                rid = region.get("id", "")

                if x2 <= x1 or y2 <= y1:
                    region_results.append(OcrRegionResult(id=rid, text=""))
                    continue

                crop = img.crop((x1, y1, x2, y2))
                crop_arr = np.array(crop)
                text, items = self._run_ocr_on_image(crop_arr)
                region_results.append(OcrRegionResult(id=rid, text=text, items=items))

            return OcrResponse(
                image_size={"width": img_w, "height": img_h},
                regions=region_results,
            )


# ---- FastAPI app ----

def create_app(runtime: OcrRuntime) -> FastAPI:
    app = FastAPI(title="PPStructureV3 OCR Service", version="2.0.0")

    @app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/predict", response_model=OcrResponse)
    async def predict(request: OcrRequest) -> OcrResponse:
        try:
            return await runtime.predict(request)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    return app


# ---- CLI ----

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start PPStructureV3 OCR HTTP service")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind")
    parser.add_argument("--port", type=int, default=8002, help="Port to bind")
    parser.add_argument("--lang", default="en", help="OCR language")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    runtime = OcrRuntime(lang=args.lang)
    uvicorn.run(create_app(runtime), host=args.host, port=args.port, workers=1)
