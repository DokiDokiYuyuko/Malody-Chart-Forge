"""One bounded recovery from an actual paired model output on the same clock."""
import copy
import math
from .advanced import SR
from .section_plan import canonical_hash


def missing_model_timing(errors):
    return any(marker in str(errors) for marker in
               ('模型未生成节拍', 'V32 没有生成有效节拍', 'No timing points found in beatmap.'))


def _reference_project(project, points, source, authorized_missing_timing=False):
    if authorized_missing_timing is not True:
        raise ValueError('节拍恢复仅允许实际缺失节拍后的单次授权重试')
    if source not in ('paired_stem_model_output','original_native_timing_model'):
        raise ValueError('Timing rescue source is not a validated model recovery')
    previous=-math.inf
    for row in points:
        if (not isinstance(row,(list,tuple)) or len(row)!=2
            or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in row)
            or row[0]<0 or row[0]<=previous or not 20<=row[1]<=600):
            raise ValueError('Timing rescue contains invalid absolute timing points')
        previous=row[0]
    tempo=project.get('tempo',{})
    timing=project.get('timing_map') or {}
    if ((tempo.get('manual') and not tempo.get('uncertain',False))
        or timing.get('eligibility',{}).get('v32')):
        raise ValueError('已确认或合格共享节拍不允许由声部恢复覆盖')
    # Compare reliable beat-frequency evidence even when bar phase was rejected.
    reliable=timing.get('fit',{})
    if reliable.get('eligible') or timing.get('eligibility',{}).get('beat'):
        bpm=reliable.get('bpm')
        if bpm and any(abs(float(row[1])/float(bpm)-1)>.03 for row in points):
            raise ValueError('恢复节拍与冻结可靠拍速不一致（含半速或倍速）')
        phase=reliable.get('phase_sample')
        if phase is not None and bpm:
            period=60000/float(bpm);phase_ms=phase*1000/project.get('sample_rate',SR)
            if any(abs((row[0]-phase_ms)/period-round((row[0]-phase_ms)/period))*period>35 for row in points):
                raise ValueError('Recovery timing phase disagrees with frozen reliable beat evidence')
    frozen = copy.deepcopy(project)
    frozen['timing_rescue']={'points':copy.deepcopy(points),'source':source,
        'authorized_missing_timing':True,'attempts':1,'confirmed':False,
        'reason':'actual_missing_model_timing'}
    return frozen


def original_recovery_project(original, directory, project, segment, progress, *, authorized_missing_timing=False):
    """When both isolated tracks lose timing, run ordinary timing on original PCM.

    No MAP inference, no reference input, no tempo estimate or synthetic grid.
    The source clock and effective end are frozen by the queued parent task.
    """
    _reference_project(project,[], 'original_native_timing_model',authorized_missing_timing)
    from .advanced import atomic, read
    from .audio_bounds import content_end
    from .beat_analysis import v32_timing
    from .resident import code_version
    import soundfile as sf
    info=sf.info(original)
    rate=project.get('sample_rate',SR)
    if info.samplerate!=rate or rate!=SR or info.frames!=project['samples']:
        raise ValueError('原曲节拍恢复音源与冻结采样时钟不符')
    source = {'pcm_sha256':project['source_pcm_sha256'], 'sample_rate':rate,
              'samples':project['samples'], 'effective_end_sample':content_end(project)}
    seed = int(canonical_hash({'source':source, 'policy':'original-native-clock-v1'})[:8],16)%2147483640
    identity = canonical_hash({'source':source,'seed':seed,'adapter':code_version()})
    path = directory/'native-timing-recovery'/(identity+'.json')
    if path.is_file():
        clock = read(path)
        if clock.get('source') != source or clock.get('hash') != canonical_hash({k:v for k,v in clock.items() if k!='hash'}):
            raise ValueError('原曲模型节拍恢复缓存校验失败')
    else:
        result = v32_timing(original,source['sample_rate'],False,progress,seed,source['effective_end_sample'])
        points = result.get('model_timing_points', [])
        if len(result.get('beat_samples',[])) < 8 or not points:
            raise ValueError('原曲模型也没有有效节拍，请确认 BPM 后重试')
        clock = {'source':source,'points':points,'seed':seed,'adapter':identity,
                 'provenance':result.get('provenance',{}), 'beat_samples':result['beat_samples'],
                 'confirmed':False,'reference_used':False}
        clock['hash'] = canonical_hash(clock)
    points=clock['points'];previous=-math.inf
    for row in points:
        if (len(row)!=2 or any(isinstance(x,bool) or not isinstance(x,(float,int)) or not math.isfinite(x) for x in row)
            or not 0 <= row[0] < source['effective_end_sample']*1000/source['sample_rate']
            or row[0]<=previous or not 20<=row[1]<=600):
            raise ValueError('原曲模型节拍坐标或拍速无效')
        previous=row[0]
    if not path.is_file():atomic(path,clock)
    record = {'source':'original_native_timing_model','source_pcm_sha256':source['pcm_sha256'],
              'reference_hash':clock['hash'],'clock':'original_absolute_ms','points':points,
              'range':[segment['start_sample'],segment['end_sample']],'confirmed':False,'attempts':1,'authorized_missing_timing':True}
    return _reference_project(project,points,'original_native_timing_model',authorized_missing_timing),record


def recovery_project(project, segment, failed_role, counterpart, manifest, variant, *, authorized_missing_timing=False):
    _reference_project(project,[], 'paired_stem_model_output',authorized_missing_timing)
    expected_role = 'accompaniment' if failed_role == 'vocals' else 'vocals'
    provenance = counterpart.get('provenance', {})
    bounds = [segment['start_sample'], segment['end_sample']]
    source = next((s for s in manifest['stems'] if s['role'] == expected_role), None)
    if (source is None or counterpart.get('kind') != 'stem_raw' or counterpart.get('variant') != variant
        or counterpart.get('range') != bounds or provenance.get('source_role') != expected_role
        or provenance.get('stem_set_id') != manifest['id'] or provenance.get('source_id') != source['source_id']
        or provenance.get('pcm_sha') != source['pcm_sha']
        or provenance.get('parent_source_id') != manifest['source_pcm_sha']
        or manifest['source_pcm_sha'] != project.get('source_pcm_sha256')
        or manifest['sample_rate'] != SR or manifest['origin_sample'] != 0
        or manifest['frame_count'] != project['samples']):
        raise ValueError('声部节拍恢复参考与冻结原曲、片段或声部身份不符')
    timing = provenance.get('serialization_tempo', {})
    points = timing.get('points', [])
    if timing.get('source') != 'model_output' or not points:
        raise ValueError('另一声部没有有效模型节拍，不使用估计 BPM 代替')
    previous = -math.inf
    for row in points:
        if (not isinstance(row, (list, tuple)) or len(row) != 2
            or any(isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x) for x in row)
            or not 0 <= row[0] <= segment.get('effective_end_sample', bounds[1])*1000/SR+1
            or row[0] <= previous or not 20 <= row[1] <= 600):
            raise ValueError('另一声部模型节拍坐标或拍速无效')
        previous = row[0]
    record = {'source': 'paired_stem_model_output', 'reference_revision_id': counterpart['id'],
              'reference_role': expected_role, 'reference_variant': variant, 'failed_role': failed_role,
              'source_pcm_sha256': manifest['source_pcm_sha'], 'stem_set_id': manifest['id'],
              'range': bounds, 'clock': 'original_absolute_ms', 'points': copy.deepcopy(points),
              'reference_hash': canonical_hash(points), 'attempts': 1, 'confirmed': False,'authorized_missing_timing':True}
    # This is an inference rescue, not a replacement for the detected TimingMap.
    # The original map stays in the task snapshot and output provenance.
    return _reference_project(project,points,'paired_stem_model_output',authorized_missing_timing), record
