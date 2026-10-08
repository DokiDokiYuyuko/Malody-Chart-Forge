"""Bounded recovery for the upstream empty timing context assertion."""
import math
from pathlib import Path


UPSTREAM_NO_TIMING = 'No timing points found in beatmap.'
RECOVERY_FAILED = '模型未生成节拍，项目参考恢复失败；请确认 BPM 后重试'


def missing_timing(error):
    return isinstance(error, AssertionError) and str(error) == UPSTREAM_NO_TIMING


def timing_empty_message(message):
    """Exact text of a per-request rejection caused by the upstream empty-timing assertion.

    Never a substring match: the worker reports either the assertion itself or the
    project's recovery-failed ValueError that wraps it."""
    return isinstance(message, str) and message in (UPSTREAM_NO_TIMING, RECOVERY_FAILED)


def valid_reference(path):
    active=False;points=[]
    for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
        if line.startswith('['): active=line.strip()=='[TimingPoints]'
        elif active and line.strip():
            values=line.split(',')
            if len(values)>=7 and values[6].strip()!='1':continue
            try:
                t,beat=float(values[0]),float(values[1])
                if not math.isfinite(t) or not math.isfinite(beat) or beat<=0 or not 20<=60000/beat<=600:raise ValueError()
                points.append(t)
            except (ValueError,IndexError):raise ValueError('原曲节拍参考无效，请确认 BPM 锚点')
    # An observed first beat is often after an intro/weak pickup. Zero is the
    # audio origin, not necessarily beat zero; never manufacture a zero anchor.
    if not points or points[0]<0 or any(b<=a for a,b in zip(points,points[1:])):raise ValueError('原曲节拍参考缺失或时间基准无效，请确认 BPM 锚点')
    return str(path)


def recovery_reference(error, key, reference, attempted):
    if not missing_timing(error):raise error
    if key in attempted or not reference:
        raise ValueError(RECOVERY_FAILED) from error
    attempted.add(key)
    return valid_reference(reference)
