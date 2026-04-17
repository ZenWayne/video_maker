"""CosyVoice Voice Conversion HTTP service."""

import io
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import torch
import torchaudio
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global model instance
cosyvoice_model = None


def _load_model():
    import sys
    import os

    # Add Matcha-TTS to path for CosyVoice internals
    cosyvoice_root = os.environ.get("COSYVOICE_ROOT", "/workspace/CosyVoice")
    matcha_path = os.path.join(cosyvoice_root, "third_party", "Matcha-TTS")
    if matcha_path not in sys.path:
        sys.path.insert(0, matcha_path)
    if cosyvoice_root not in sys.path:
        sys.path.insert(0, cosyvoice_root)

    from cosyvoice.cli.cosyvoice import AutoModel

    model_dir = os.environ.get(
        "COSYVOICE_MODEL_DIR", "FunAudioLLM/Fun-CosyVoice3-0.5B-2512"
    )
    logger.info("Loading CosyVoice model from %s ...", model_dir)
    model = AutoModel(model_dir=model_dir)
    logger.info("CosyVoice model loaded (sample_rate=%d)", model.sample_rate)
    return model


@asynccontextmanager
async def lifespan(app: FastAPI):
    global cosyvoice_model
    cosyvoice_model = _load_model()
    yield


app = FastAPI(title="CosyVoice VC Service", lifespan=lifespan)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "model_loaded": cosyvoice_model is not None,
    }


@app.post("/vc")
async def voice_convert(
    source_audio: UploadFile = File(...),
    prompt_audio: UploadFile = File(...),
):
    """Run voice conversion: convert source_audio to match prompt_audio's timbre."""
    if cosyvoice_model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        # Save uploaded files
        src_path = tmp_dir / "source.wav"
        ref_path = tmp_dir / "prompt.wav"
        src_path.write_bytes(await source_audio.read())
        ref_path.write_bytes(await prompt_audio.read())

        # Run voice conversion
        logger.info("Running VC: source=%s, prompt=%s", src_path, ref_path)
        results = list(
            cosyvoice_model.inference_vc(
                str(src_path), str(ref_path), stream=False
            )
        )

        if not results:
            raise HTTPException(status_code=500, detail="Voice conversion produced no output")

        # Concatenate all chunks (usually just one)
        speeches = [r["tts_speech"] for r in results]
        combined = torch.cat(speeches, dim=1) if len(speeches) > 1 else speeches[0]

        # Save to buffer as WAV
        buf = io.BytesIO()
        torchaudio.save(buf, combined, cosyvoice_model.sample_rate, format="wav")
        buf.seek(0)

        logger.info(
            "VC done: %.2f seconds output",
            combined.shape[1] / cosyvoice_model.sample_rate,
        )

        return StreamingResponse(
            buf,
            media_type="audio/wav",
            headers={"Content-Disposition": "attachment; filename=vc_output.wav"},
        )
