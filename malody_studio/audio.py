from pathlib import Path
import subprocess
import numpy as np
import soundfile as sf
import librosa
import imageio_ffmpeg

def ffmpeg():
    return imageio_ffmpeg.get_ffmpeg_exe()

def convert(source, directory, tail_trim_enabled=False):
    directory = Path(directory)
    directory.mkdir(exist_ok=True)
    wave = directory / 'analysis.wav'
    from .audio_bounds import detect_tail, SR
    import json
    original = directory / 'source.wav'
    result = subprocess.run([ffmpeg(),'-hide_banner','-loglevel','error','-y','-i',str(source),'-vn','-ar',str(SR),'-c:a','pcm_f32le',str(original)],capture_output=True,text=True,timeout=120)
    if result.returncode:raise ValueError('音频解码失败：'+result.stderr[-300:])
    trim = {**detect_tail(original),'enabled':tail_trim_enabled}
    data, _ = sf.read(original,dtype='float32',always_2d=True)
    if not 5 <= len(data)/SR <= 600:raise ValueError('请上传 5 秒至 10 分钟的音乐')
    end = trim['cutoff_sample'] if tail_trim_enabled else len(data)
    if not end or not np.isfinite(data).all() or np.max(np.abs(data)) < 1e-5:raise ValueError('音频无有效声音')
    retained = directory / 'retained.wav'
    sf.write(retained,data[:end],SR,subtype='FLOAT')
    (directory/'tail-analysis.json').write_text(json.dumps(trim),encoding='utf-8')
    for destination, options in [(wave, ['-ac', '1', '-ar', '22050', '-c:a', 'pcm_f32le']),
                                  (directory / 'audio.ogg', ['-ac', '2', '-ar', '44100', '-c:a', 'libvorbis', '-q:a', '5'])]:
        result = subprocess.run([ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y', '-i', str(retained),
                                 '-vn', *options, str(destination)], capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise ValueError('音频解码失败，请使用有效的 MP3、WAV、FLAC、M4A 或 OGG 文件。' + result.stderr[-300:])
    y, sr = sf.read(wave, dtype='float32')
    duration = end / SR
    if not np.isfinite(y).all() or np.max(np.abs(y)) < 1e-5:
        raise ValueError('音频无有效声音')
    encoded = sf.info(directory / 'audio.ogg')
    if encoded.format != 'OGG' or encoded.subtype != 'VORBIS':
        raise ValueError('游戏音频必须为 OGG Vorbis')
    if abs(encoded.frames - end) > 1:
        raise ValueError('转换后音频时间轴发生变化')
    return y, sr, duration, directory / 'audio.ogg'

def analyze(y, sr, bpm_override=None):
    hop = 256
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    tempo, frames = librosa.beat.beat_track(onset_envelope=onset, sr=sr, hop_length=hop, trim=False)
    beats = librosa.frames_to_time(frames, sr=sr, hop_length=hop)
    warnings = []
    bpm = float(np.asarray(tempo).reshape(-1)[0])
    if bpm_override is not None:
        bpm = bpm_override
    if not np.isfinite(bpm) or bpm <= 0 or (len(beats) < 3 and bpm_override is None):
        raise ValueError('未识别到可靠节拍，请填写已知 BPM 后重试' if bpm_override is None else '未识别到有效节奏，请检查音乐')
    intervals = np.diff(beats)
    variability = float(np.std(intervals) / max(1e-9, np.mean(intervals))) if len(intervals) else 0
    # BPM is editor metadata. Never move model notes just to fit an uncertain grid.
    if variability > 0.04:
        warnings.append('节拍可能有变化：BPM 用于编辑器显示，音符保留模型生成的实际时间，不强制吸附。')
    if bpm_override is None:
        warnings.append('自动 BPM 可能存在半速或倍速差异，可填写已知 BPM；不会因此改变音符实际时间。')
    elif len(beats) < 3:
        warnings.append('自动节拍未识别成功，编辑器使用填写的 BPM；请结合音乐检查生成音符。')
    chunks = np.array_split(y, 420)
    waveform = [round(float(np.max(np.abs(chunk))), 4) if len(chunk) else 0 for chunk in chunks]
    return {'bpm': round(bpm, 3), 'beat_variability': round(variability, 4),
            'beat_times': [round(float(b), 4) for b in beats], 'waveform': waveform,
            'warnings': warnings}
