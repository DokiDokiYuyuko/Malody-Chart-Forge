"""Validated, atomic delivery copies. Source projects and caches stay immutable."""
import copy
import hashlib
import json
import os
import shutil
import threading
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .naming import archive_stem, chart_label, safe_component, validate_creator
from .charts import validate_chart

VERSION=2
_lock=threading.RLock()


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)


def home(root):return Path(root)/'outputs'/'曲包'


def index(root):
    path=home(root)/'index.json'
    return _read(path) if path.is_file() else {}


def tombstones(root):
    path=home(root)/'deleted.json'
    return _read(path) if path.is_file() else {}


def is_deleted(root,record_id):
    return record_id in tombstones(root)


def locate(root,record_id):
    if is_deleted(root,record_id):raise ValueError('曲包已删除，请明确重新导出')
    row=index(root).get(record_id)
    if not row:raise ValueError('曲包尚未整理或记录不存在')
    base=home(root).resolve();folder=(base/row['directory']).resolve()
    if not folder.is_relative_to(base) or folder==base:raise ValueError('曲包目录无效')
    manifest=_read(folder/'manifest.json')
    if manifest['record_id']!=record_id:raise ValueError('曲包记录身份不匹配')
    target=(folder/manifest['archive']).resolve()
    if target.parent!=folder or not target.is_file():raise ValueError('曲包文件缺失')
    return folder,manifest,target


def publish(root,record_id,archive,report,created=None,source=None):
    if is_deleted(root,record_id):raise ValueError('曲包已删除，请明确重新导出')
    archive=Path(archive)
    with archive.open('rb') as stream:source_hash=hashlib.file_digest(stream,'sha256').hexdigest()
    report=copy.deepcopy(report)
    if (source or {}).get('type')=='advanced' and report['title'].endswith('（剪辑版）'):
        report['legacy_title']=report['title']
        report['title']=report['title'][:-5]
    charts=report.get('charts') or report.get('difficulties') or []
    if not charts:raise ValueError('成品报告缺少谱面组合')
    for row in charts:
        row.setdefault('pattern','balanced')
        row.setdefault('difficulty',row['key'].split('--')[-1])
        if row['difficulty']=='normal':row['difficulty']='medium'
        row.setdefault('chart_id',row['pattern']+'--'+row['difficulty'])
    report['charts']=charts
    title=report['title'];artist=report.get('artist','')
    identity_fields=[source_hash,title,artist,VERSION]
    if 'creator' in report:
        report['creator']=validate_creator(report['creator'])
        identity_fields.append(report['creator'])
    row_creators={r['chart_id']:validate_creator(r['creator']) for r in charts if 'creator' in r}
    if row_creators:identity_fields.append(row_creators)
    identity=hashlib.sha256(json.dumps(identity_fields,ensure_ascii=False).encode()).hexdigest()
    with _lock:
        if is_deleted(root,record_id):raise ValueError('曲包已删除，请明确重新导出')
        catalog=index(root)
        previous_folder=None
        if record_id in catalog:
            folder,manifest,target=locate(root,record_id)
            previous_folder=folder
            if manifest.get('identity')==identity:
                valid=True
                for filename,digest in manifest['files'].items():
                    item=folder/filename
                    if item.parent!=folder or not item.is_file():valid=False;break
                    with item.open('rb') as f:
                        if hashlib.file_digest(f,'sha256').hexdigest()!=digest:valid=False;break
                if valid:return folder,manifest,target
        timestamp=datetime.fromisoformat((created or report.get('created') or datetime.now(timezone.utc).isoformat()).replace('Z','+00:00'))
        if timestamp.tzinfo is None:timestamp=timestamp.replace(tzinfo=timezone.utc)
        prefix=timestamp.astimezone(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
        token=hashlib.sha256(record_id.encode()).hexdigest()
        parent=home(root)/safe_component(title,72);parent.mkdir(parents=True,exist_ok=True)
        for length in range(12,65,4):
            destination=parent/(prefix+'-'+token[:length])
            if not destination.exists() or _read(destination/'manifest.json').get('record_id')==record_id:break
        staging=parent/('.partial-'+uuid.uuid4().hex);staging.mkdir()
        try:
            patterns=[row['pattern'] for row in charts];difficulties=[row.get('difficulty') or row['key'].split('--')[-1] for row in charts]
            name=archive_stem(title,'',patterns,difficulties)+'.mcz'
            fixed=copy.deepcopy(report);fixed.update(title=title,artist=artist,download_name=name,naming_version=VERSION)
            filenames={}
            with zipfile.ZipFile(archive) as old:
                if old.testzip():raise ValueError('原曲包 CRC 校验失败')
                members=old.namelist();mc=[n for n in members if n.lower().endswith('.mc')]
                if len(mc)!=len(charts):raise ValueError('谱面数量与报告不一致')
                parsed={}
                for entry in mc:
                    c=json.loads(old.read(entry).decode('utf-8-sig'));parsed[entry]=c
                with zipfile.ZipFile(staging/name,'w',zipfile.ZIP_DEFLATED) as new:
                    audio_members=set();background_members=set();used=set()
                    for row in charts:
                        key=row.get('chart_id') or row['key'];difficulty=row.get('difficulty') or key.split('--')[-1]
                        entry=next((n for n in mc if n not in used and (Path(n).name==row.get('filename') or Path(n).stem in (key,row['key']))),None)
                        if entry is None:
                            entry=next((n for n in mc if n not in used and parsed[n]['meta'].get('version') in (key,chart_label(row['pattern'],difficulty))),None)
                        if entry is None and len(mc)==1:entry=mc[0]
                        if entry is None:raise ValueError('无法确认谱面组合对应文件：'+key)
                        used.add(entry);c=copy.deepcopy(parsed[entry]);c['meta']['version']=chart_label(row['pattern'],difficulty)
                        c['meta'].setdefault('song',{}).update(title=title,artist=artist)
                        if key in row_creators:c['meta']['creator']=row_creators[key]
                        elif 'creator' in report:c['meta']['creator']=report['creator']
                        bgm=[n for n in c['note'] if n.get('type')==1 and 'sound' in n]
                        if len(bgm)!=1:raise ValueError('谱面需要唯一背景音乐引用')
                        import posixpath
                        def mcz_member_path(value,parent):
                            value=str(value).replace('\\','/')
                            path=posixpath.normpath(posixpath.join(parent,value))
                            if value.startswith('/') or ':' in value or path.startswith('../'):raise ValueError('曲包引用越过包内目录')
                            return path
                        parent_name=entry.rsplit('/',1)[0] if '/' in entry else ''
                        audio_members.add(mcz_member_path(bgm[0]['sound'],parent_name));bgm[0]['sound']='audio.ogg'
                        if c['meta'].get('background'):
                            background_members.add(mcz_member_path(c['meta']['background'],parent_name));c['meta']['background']='background.jpg'
                        validate_chart(c,report['duration']*1000)
                        filename=key+'.mc';new.writestr('0/'+filename,json.dumps(c,ensure_ascii=False))
                        filenames[key]=filename;row_copy=next(r for r in fixed.get('charts',fixed.get('difficulties',[])) if (r.get('chart_id') or r['key'])==key);row_copy['filename']=filename
                    if len(audio_members)!=1 or len(background_members)>1:raise ValueError('曲包音源或封面引用不唯一')
                    audio_data=old.read(next(iter(audio_members)))
                    import soundfile as sf
                    from io import BytesIO
                    info=sf.info(BytesIO(audio_data))
                    if info.subtype!='VORBIS':raise ValueError('曲包音乐必须为 OGG Vorbis')
                    new.writestr('0/audio.ogg',audio_data)
                    if background_members:new.writestr('0/background.jpg',old.read(next(iter(background_members))))
            with zipfile.ZipFile(staging/name) as z:
                if z.testzip():raise ValueError('兼容曲包校验失败')
            if 'difficulties' in fixed:fixed['difficulties']=fixed['charts']
            fixed['filenames']=filenames;_write(staging/'report.json',fixed)
            hashes={}
            for f in (staging/name,staging/'report.json'):
                with f.open('rb') as stream:hashes[f.name]=hashlib.file_digest(stream,'sha256').hexdigest()
            manifest={'schema':1,'naming_version':VERSION,'record_id':record_id,'source':source or {},'source_archive':str(archive.resolve()),'source_sha256':source_hash,'identity':identity,'title':title,'artist':artist,'created':timestamp.isoformat(),'charts':[{'key':r.get('chart_id') or r['key'],'pattern':r['pattern'],'difficulty':r.get('difficulty') or r['key'].split('--')[-1]} for r in charts],'archive':name,'files':hashes}
            _write(staging/'manifest.json',manifest)
            retired=None
            if destination.exists():
                retired=parent/('.retired-'+uuid.uuid4().hex);destination.replace(retired)
            try:staging.replace(destination)
            except Exception:
                if retired:retired.replace(destination)
                raise
            catalog[record_id]={'directory':str(destination.relative_to(home(root))),'title':title,'artist':artist,
                                'source':source or {},'created':timestamp.isoformat()}
            _write(home(root)/'index.json',catalog)
            if retired:shutil.rmtree(retired)
            if previous_folder and previous_folder!=destination.resolve() and previous_folder.exists():
                shutil.rmtree(previous_folder)
            return destination,manifest,destination/name
        finally:
            if staging.exists():shutil.rmtree(staging)


def remove(root,record_id):
    with _lock:
        catalog=index(root)
        if record_id not in catalog:return
        folder,_,_=locate(root,record_id)
        shutil.rmtree(folder);catalog.pop(record_id);_write(home(root)/'index.json',catalog)


def delete_delivery(root,record_id,source=None):
    """Delete only a registered delivery, retaining a durable identity tombstone.

    Rename first: a permissions failure leaves the catalog and identity intact.
    Cleanup after commit can be retried without reviving a deleted package.
    """
    with _lock:
        catalog=index(root);deleted=tombstones(root)
        if record_id in deleted:
            result={'record_id':record_id,'deleted':True,'already_deleted':True}
            cleanup=deleted[record_id].get('cleanup_directory')
            if cleanup:
                base=home(root).resolve();pending=(base/cleanup).resolve()
                if not pending.is_relative_to(base) or not pending.name.startswith('.deleted-'):raise ValueError('曲包清理目录无效')
                if pending.exists():
                    try:shutil.rmtree(pending)
                    except OSError as exc:result.update(cleanup_pending=True,error='曲包已移出库，文件清理待重试：'+str(exc))
            if record_id in catalog:
                catalog.pop(record_id);_write(home(root)/'index.json',catalog)
            return result
        row=catalog.get(record_id);folder=None;retired=None
        if row:
            base=home(root).resolve();folder=(base/row['directory']).resolve()
            if folder==base or not folder.is_relative_to(base):raise ValueError('曲包目录无效')
            if folder.exists():
                manifest=_read(folder/'manifest.json')
                if manifest.get('record_id')!=record_id:raise ValueError('曲包记录身份不匹配')
                retired=folder.parent/('.deleted-'+uuid.uuid4().hex)
                folder.replace(retired)
        elif source is None:
            raise ValueError('曲包记录不存在')
        deleted[record_id]={'deleted_at':datetime.now(timezone.utc).isoformat(),
                            'source':source or (row or {}).get('source',{}),
                            'directory':(row or {}).get('directory'),
                            'cleanup_directory':str(retired.relative_to(home(root))) if retired else None}
        try:
            _write(home(root)/'deleted.json',deleted)
        except Exception:
            if retired:retired.replace(folder)
            raise
        # A tombstone is authoritative even if writing the secondary catalog fails.
        result={'record_id':record_id,'deleted':True}
        catalog.pop(record_id,None)
        try:_write(home(root)/'index.json',catalog)
        except OSError as exc:result.update(catalog_cleanup_pending=True,error='曲包已移出库，登记清理待重试：'+str(exc))
        if retired:
            try:shutil.rmtree(retired)
            except OSError as exc:result.update(cleanup_pending=True,error='曲包已移出库，文件清理待重试：'+str(exc))
        return result


def open_folder(root,record_id):
    folder,_,_=locate(root,record_id)
    if os.name=='nt':
        from .folder_open import open_registered_directory
        open_registered_directory(folder)
    else:raise ValueError('打开文件夹仅支持 Windows 本机服务')
    return {'opened':True,'record_id':record_id}
