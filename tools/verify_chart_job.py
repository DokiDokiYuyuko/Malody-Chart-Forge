"""Independently reconstruct MC audio times and verify the downloadable archive."""
import argparse
import bisect
import io
import json
from pathlib import Path
import zipfile
import hashlib
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]

def beat(value):
    return value[0] + value[1] / value[2]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('job')
    parser.add_argument('--annotate-v32', action='store_true')
    args = parser.parse_args()
    if len(args.job) != 32 or any(c not in '0123456789abcdef' for c in args.job):
        raise ValueError('Invalid job identifier')
    directory = ROOT / 'outputs' / args.job
    report_path = directory / 'report.json'
    report = json.loads(report_path.read_text(encoding='utf-8'))
    job = json.loads((directory / 'job.json').read_text(encoding='utf-8'))
    max_error, totals, background_verified = 0, {}, False
    with zipfile.ZipFile(directory / 'malody-4k.mcz') as archive:
        assert archive.testzip() is None
        audio = sf.info(io.BytesIO(archive.read('0/audio.ogg')))
        assert audio.format == 'OGG' and audio.subtype == 'VORBIS' and audio.samplerate == 44100 and audio.channels == 2
        assert abs(audio.duration - report['duration']) < .02
        if report.get('artwork', {}).get('status') == 'ready':
            from PIL import Image
            background = archive.read('0/background.jpg')
            with Image.open(io.BytesIO(background)) as picture:
                assert picture.format == 'JPEG' and list(picture.size) == report['artwork']['dimensions']
                picture.verify()
            assert hashlib.sha256(background).hexdigest() == report['artwork']['sha256']
            background_verified = True
        for result in report['difficulties']:
            key = result['key']
            chart = json.loads(archive.read('0/' + key + '.mc'))
            assert chart == json.loads((directory / '0' / (key + '.mc')).read_text(encoding='utf-8'))
            assert chart['meta']['mode'] == 0 and chart['meta']['mode_ext']['column'] == 4
            if background_verified:
                assert chart['meta']['background'] == 'background.jpg'
            bgm = next(n for n in chart['note'] if n.get('type') == 1)
            points = chart['time']
            origins = [beat(p['beat']) for p in points]
            elapsed = [-bgm['offset']]
            for index in range(1, len(points)):
                elapsed.append(elapsed[-1] + (origins[index] - origins[index - 1]) * 60000 / points[index - 1]['bpm'])
            def milliseconds(value):
                target = beat(value)
                index = max(0, bisect.bisect_right(origins, target) - 1)
                return elapsed[index] + (target - origins[index]) * 60000 / points[index]['bpm']
            events = [event for event in chart['note'] if 'column' in event]
            assert len(events) == len(report['previews'][key]) == result['notes']
            for event, preview in zip(events, report['previews'][key]):
                assert event['column'] == preview[1]
                max_error = max(max_error, abs(milliseconds(event['beat']) - preview[0]))
                if preview[2] is not None:
                    max_error = max(max_error, abs(milliseconds(event['endbeat']) - preview[2]))
            totals[key] = {'notes': len(events), 'bpm_points': len(points)}
    assert max_error < 1.0, f'MC timing reconstruction error: {max_error} ms'
    verification = {'job': args.job, 'archive_valid': True, 'audio_format': 'OGG Vorbis / 44100 / stereo',
                    'maximum_timing_error_ms': round(max_error, 6), 'background_verified': background_verified, 'charts': totals}
    (directory / 'independent-verification.json').write_text(json.dumps(verification, indent=2), encoding='utf-8')
    if args.annotate_v32:
        assert report['engine'] == 'Mapperatorinator V32 mania'
        target = job['options']['ln_ratio']
        report['requested_ln_ratio'] = target
        report['warnings'] = [warning for warning in report['warnings'] if '可填写已知 BPM' not in warning and '实际长条比例' not in warning]
        for result in report['difficulties']:
            if abs(result['ln_ratio'] - target) > .15:
                report['warnings'].append(f"{result['label']}实际长条比例 {result['ln_ratio']:.1%}，与目标 {target:.1%} 偏差较大；生成条件不保证实际比例。")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        job['report'] = report
        (directory / 'job.json').write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
        marker_path = ROOT / 'models' / 'mapperatorinator' / 'deployment-ready.json'
        marker = json.loads(marker_path.read_text(encoding='utf-8'))
        marker.update(full_reference_job=args.job, independent_verification=verification,
                      validated_environment='Python 3.12.14 / torch 2.10.0+cu130')
        marker_path.write_text(json.dumps(marker, indent=2), encoding='utf-8')
    print(json.dumps(verification, indent=2))

if __name__ == '__main__':
    main()
