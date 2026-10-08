from types import SimpleNamespace
import pytest
from tools.mapperatorinator_worker import compile_generation_args
from malody_studio.v32_recovery import valid_reference, recovery_reference


def test_clock_reference_does_not_inherit_chart_conditioning(tmp_path):
    reference=tmp_path/'clock.osu'
    reference.write_text('[Metadata]\nTitle:Clock only\n[Difficulty]\nApproachRate:5\n[TimingPoints]\n2200,500,3,2,1,100,1,0\n')
    seen=[]
    def compiler(args):
        seen.append(args.beatmap_path)
        # Model upstream's metadata-import branch, including None style ids.
        args.hitsounded=not bool(args.beatmap_path)
        args.approach_rate=5 if args.beatmap_path else 9
        if args.beatmap_path:args.mapper_id=123
    baseline=SimpleNamespace(beatmap_path='',in_context=[],mapper_id=None,difficulty=5.9,title='Song')
    timed=SimpleNamespace(**vars(baseline))
    compile_generation_args(baseline,{},compiler,'TIMING')
    compile_generation_args(timed,{'timing_reference':str(reference)},compiler,'TIMING')
    assert seen==['','']
    assert {k:v for k,v in vars(timed).items() if k not in ('beatmap_path','in_context')}=={
        k:v for k,v in vars(baseline).items() if k not in ('beatmap_path','in_context')}
    assert timed.in_context==['TIMING'] and timed.beatmap_path==str(reference.resolve())
    assert timed.difficulty==5.9 and timed.mapper_id is None and timed.hitsounded


def test_missing_clock_reference_rejected_before_compile(tmp_path):
    with pytest.raises(ValueError,match='参考文件'):
        compile_generation_args(SimpleNamespace(),{'timing_reference':str(tmp_path/'missing.osu')},
                                lambda _:pytest.fail('must not compile'), 'TIMING')


def test_recovery_preserves_nonzero_first_beat_and_is_bounded(tmp_path):
    reference=tmp_path/'clock.osu'
    reference.write_text('[TimingPoints]\n2200,500,3,2,1,100,1,0\n')
    before=reference.read_bytes();attempted=set()
    error=AssertionError('No timing points found in beatmap.')
    assert recovery_reference(error,'expert',str(reference),attempted)==str(reference)
    assert reference.read_bytes()==before
    with pytest.raises(ValueError,match='恢复失败'):
        recovery_reference(error,'expert',str(reference),attempted)
    reference.write_text('[TimingPoints]\n-1,500,4,2,1,100,1,0\n')
    with pytest.raises(ValueError):valid_reference(reference)
