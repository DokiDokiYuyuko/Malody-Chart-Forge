"""Stateless MuG requests share resident weights, never another job's wave or seed."""
from pathlib import Path
import shutil
import os
import uuid
import numpy as np
from .paths import ROOT
from .resident import call,atomic
from .charts import Note


class RemoteEngine:
    def __init__(self):
        self.device='cuda';self.directory=ROOT/'cache/resident-inputs'/uuid.uuid4().hex
        self.directory.mkdir(parents=True);self.model=None;self.telemetry=[]
    def load(self,progress):pass
    def prepare(self,y,sr,progress):
        input=self.directory/(uuid.uuid4().hex+'.npy');output=input.with_name(input.stem+'-wave.npy')
        np.save(input,y,allow_pickle=False)
        result=call('mug',{'action':'prepare','input':str(input),'output':str(output),'sr':sr},progress)
        input.unlink(missing_ok=True);self.telemetry.append(result['resident'])
        return {'path':str(output),'z_length':result['z_length'],'wave_kind':result.get('wave_kind','tensor')}
    def generate(self,wave,preset,options,progress):
        result=call('mug',{'action':'generate','input':wave['path'],'z_length':wave['z_length'],'wave_kind':wave['wave_kind'],'options':options},lambda message,percent:progress(max(0,min(1,(percent-20)/65))))
        self.telemetry.append(result['resident']);return [Note(*note) for note in result['notes']]
    def unload(self):
        job=Path(os.environ.get('STARTRAIL_JOB_DIRECTORY',str(ROOT/'cache/resident-inputs')))
        job.resolve().relative_to(ROOT)
        telemetry=job/('resident-mug-'+self.directory.name+'.json')
        atomic(telemetry,{'engine':'mug','requests':self.telemetry})
        shutil.rmtree(self.directory)
