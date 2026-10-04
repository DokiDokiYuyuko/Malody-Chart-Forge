"""Decode model-input WAV without ffprobe, preserving FLOAT stem amplitudes."""
import math
import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly


def load_wave(path, sample_rate, speed=1., normalize=True):
    rate, data = wavfile.read(str(path))
    if speed != 1.:
        raise ValueError('Frozen source-time inference does not permit speed changes')
    if data.dtype.kind == 'u':
        data=(data.astype(np.float32)-128)/128
    elif data.dtype.kind == 'i':
        data=data.astype(np.float32)/float(2**(data.dtype.itemsize*8-1))
    else:data=data.astype(np.float32)
    if data.ndim==2:data=data.mean(axis=1)
    if data.ndim!=1 or not len(data) or not np.isfinite(data).all():
        raise ValueError('Model input PCM must be finite and nonempty')
    if rate!=sample_rate:
        divisor=math.gcd(rate,sample_rate)
        data=resample_poly(data,sample_rate//divisor,rate//divisor).astype(np.float32)
    # Preserve upstream model feature normalization; project stem files and
    # acoustic salience remain on their original common amplitude scale.
    peak=np.max(np.abs(data))
    if normalize and peak>0:data=data/peak
    return np.ascontiguousarray(data,dtype=np.float32)
