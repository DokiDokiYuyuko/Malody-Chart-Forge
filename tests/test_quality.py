from malody_studio.charts import Note, serialize
from malody_studio.quality import assess
import numpy as np


def test_quality_alerts_are_timestamped_and_do_not_change_notes():
    notes=[Note(float(2000+i*100),i%4) for i in range(30)]
    chart=serialize(notes,'Song','Artist','4K Test',120)
    chart['note']=[{'beat':[0,0,1],'column':0},{'beat':[0,1,48],'column':1}]
    audio=np.concatenate([np.ones(22050,dtype=np.float32)*.2,np.zeros(3*22050,dtype=np.float32)])
    alerts=assess(notes,5,.5,audio,22050,chart,'lunatic')
    types={item['type'] for item in alerts}
    assert {'local_density','notes_in_silence','fine_subdivision'}<=types
    assert all(item['difficulty']=='lunatic' and item['start_ms']>=0 for item in alerts)


def test_active_audio_without_notes_is_reported_without_inventing_notes():
    audio=np.concatenate([np.zeros(22050,dtype=np.float32),np.full(22050*4,.08,dtype=np.float32)])
    alerts=assess([],5,5,audio,22050,difficulty='hard')
    gap=[item for item in alerts if item['type']=='active_audio_without_notes']
    assert gap and gap[0]['start_ms']==0 and gap[0]['difficulty']=='hard'
    assert '试听确认' in gap[0]['message']


def test_active_audio_with_only_one_note_is_reported_as_underfilled():
    from malody_studio.charts import Note
    audio=np.full(22050*8,.08,dtype=np.float32)
    alerts=assess([Note(100,0)],8,5,audio,22050,difficulty='medium')
    sparse=[item for item in alerts if item['type']=='active_audio_underfilled']
    assert sparse and sparse[0]['start_ms']==0 and sparse[0]['metric']==.25
    assert '只有 1 个音符' in sparse[0]['message']


def test_silence_without_notes_does_not_create_missing_chart_alert():
    alerts=assess([],4,5,np.zeros(22050*4,dtype=np.float32),22050,difficulty='medium')
    assert not alerts
