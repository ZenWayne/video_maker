import sys
sys.path.append('third_party/Matcha-TTS')
import torchaudio
from cosyvoice.cli.cosyvoice import AutoModel


def convert_to_wav(input_path):
    """Convert any audio to 16kHz mono wav using ffmpeg."""
    if input_path.endswith('.wav'):
        return input_path
    import subprocess
    import imageio_ffmpeg
    wav_path = input_path.rsplit('.', 1)[0] + '.wav'
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run([ffmpeg, '-y', '-i', input_path, '-ac', '1', wav_path],
                   check=True, capture_output=True)
    return wav_path


cosyvoice = AutoModel(model_dir='FunAudioLLM/Fun-CosyVoice3-0.5B-2512')

source_wav = convert_to_wav('./output_2.m4a')
prompt_wav = convert_to_wav('./output.m4a')

for i, result in enumerate(cosyvoice.inference_vc(source_wav, prompt_wav, stream=False)):
    torchaudio.save(f'vc_output_{i}.wav', result['tts_speech'], cosyvoice.sample_rate)
