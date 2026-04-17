"""
Minimal Voice Conversion wrapper around CosyVoice3.
Keeps rhythm, pauses, breath — only swaps timbre.

Usage:
    from vc import VoiceConverter
    vc = VoiceConverter()
    vc.convert('source.wav', 'target_voice.wav', 'output.wav')
"""
import sys
import os
import subprocess

sys.path.append(os.path.join(os.path.dirname(__file__), 'third_party', 'Matcha-TTS'))

import torch
import torchaudio
import imageio_ffmpeg
import ffmpeg 


class VoiceConverter:
    def __init__(self, model_dir='FunAudioLLM/Fun-CosyVoice3-0.5B-2512'):
        from cosyvoice.cli.cosyvoice import AutoModel
        self.model = AutoModel(model_dir=model_dir)

    @staticmethod
    def _ensure_wav(path):
        if path.lower().endswith('.wav'):
            return path
        wav_path = path.rsplit('.', 1)[0] + '.wav'
        #subprocess.run([ffmpeg, '-y', '-i', path, '-ac', '1', wav_path],
        #check=True, capture_output=True)
        ffmpeg.input(path).output(wav_path, ac=1).overwrite_output().run(capture_stdout=True, capture_stderr=True)
        return wav_path

    def convert(self, source_audio, prompt_audio, output_path='vc_output.wav', stream=False):
        """
        Voice conversion: keep source content/rhythm, apply prompt's timbre.

        Args:
            source_audio: path to source audio (any format ffmpeg supports)
            prompt_audio: path to target voice sample (any format, ≤30s)
            output_path:  output wav path
            stream:       whether to use streaming inference
        """
        source_wav = self._ensure_wav(source_audio)
        prompt_wav = self._ensure_wav(prompt_audio)

        chunks = []
        for result in self.model.inference_vc(source_wav, prompt_wav, stream=stream):
            chunks.append(result['tts_speech'])

        speech = torch.cat(chunks, dim=-1)
        torchaudio.save(output_path, speech, self.model.sample_rate)
        return output_path


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Voice Conversion')
    parser.add_argument('source', help='Source audio file')
    parser.add_argument('prompt', help='Target voice sample')
    parser.add_argument('-o', '--output', default='vc_output.wav', help='Output path')
    parser.add_argument('--model', default='FunAudioLLM/Fun-CosyVoice3-0.5B-2512', help='Model ID')
    args = parser.parse_args()

    vc = VoiceConverter(model_dir=args.model)
    vc.convert(args.source, args.prompt, args.output)
    print(f'Saved to {args.output}')
