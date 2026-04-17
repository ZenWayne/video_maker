import sys, time
sys.path.append('third_party/Matcha-TTS')

import torch
# Patch before any CosyVoice import so both PyTorch and ONNX Runtime use CPU
torch.cuda.is_available = lambda: False

print('CUDA available (patched):', torch.cuda.is_available())
print('CPU threads:', torch.get_num_threads())

import torchaudio
from cosyvoice.cli.cosyvoice import AutoModel

t0 = time.time()
model = AutoModel(model_dir='FunAudioLLM/Fun-CosyVoice3-0.5B-2512')
load_time = time.time() - t0
print(f'Model load: {load_time:.1f}s', flush=True)

t1 = time.time()
chunks = []
for r in model.inference_vc('output_2.wav', 'output.wav', stream=False):
    chunks.append(r['tts_speech'])
infer_time = time.time() - t1

speech = torch.cat(chunks, dim=-1)
audio_len = speech.shape[1] / model.sample_rate
print(f'Audio length: {audio_len:.1f}s')
print(f'Inference time: {infer_time:.1f}s')
print(f'RTF: {infer_time/audio_len:.2f}x  (<1 = faster than real-time)')
torchaudio.save('vc_cpu_test.wav', speech, model.sample_rate)
print('Done! Saved: vc_cpu_test.wav')
