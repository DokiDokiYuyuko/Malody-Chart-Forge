"""HTTP boundary for downloads independent of the generation queue."""
from pathlib import Path
from uuid import uuid4
from functools import wraps
import inspect
from fastapi import APIRouter, Body, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from . import music_assets as assets
from .folder_open import open_registered_directory

router = APIRouter(prefix='/api/music-assets', tags=['music-assets'])


def boundary(function):
    def failure(exc):
        if isinstance(exc, ValueError):
            message = str(exc)
            status = 413 if '200 MB' in message else 404 if '不存在' in message else 409 if any(word in message for word in ('尚未', '完整性', '队列已满')) else 400
            detail = {'message': message}
            if hasattr(exc, 'diagnostic_id'): detail['diagnostic_id'] = exc.diagnostic_id
            raise HTTPException(status, detail) from exc
        diagnostic = assets.diagnostic('本地音乐文件操作失败：' + str(exc), exc)
        raise HTTPException(409, {'message': str(diagnostic), 'diagnostic_id': diagnostic.diagnostic_id}) from exc
    if inspect.iscoroutinefunction(function):
        @wraps(function)
        async def asynchronous(*args, **kwargs):
            try: return await function(*args, **kwargs)
            except (ValueError, OSError, RuntimeError) as exc: failure(exc)
        return asynchronous
    @wraps(function)
    def synchronous(*args, **kwargs):
        try: return function(*args, **kwargs)
        except (ValueError, OSError, RuntimeError) as exc: failure(exc)
    return synchronous


@router.post('/resolve')
@boundary
def resolve(payload: dict = Body(...)):
    return assets.store.resolve(payload.get('input', ''))


@router.get('')
def listing():
    return {'assets': assets.store.assets()}


@router.post('/downloads')
@boundary
def download(payload: dict = Body(...)):
    return assets.store.begin(payload.get('source_id'), payload.get('title'), payload.get('artist', ''))


@router.get('/downloads')
def tasks():
    return {'tasks': assets.store.tasks()}


@router.get('/downloads/{task_id}')
@boundary
def task(task_id: str):
    return assets.store.task(task_id)


@router.post('/identify')
@boundary
async def identify(file: UploadFile = File(...)):
    path = assets.store.registry / ('identify-' + uuid4().hex)
    try:
        count = 0
        with path.open('wb') as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk: break
                count += len(chunk)
                if count > assets.MAX_BYTES:
                    raise HTTPException(413, '音乐文件超过 200 MB')
                output.write(chunk)
        return {'asset': assets.identify(path,file.filename)}
    finally:
        path.unlink(missing_ok=True)


@router.post('/open-folder')
@boundary
def open_folder(payload: dict = Body(default={})):
    if payload: raise ValueError('打开音乐目录不接受客户端路径或其他参数')
    directory = assets.store.directory.resolve()
    if directory != (assets.store.root / 'data' / '音乐').resolve():
        raise ValueError('音乐目录无效')
    open_registered_directory(directory)
    return {'requested': True, 'path': str(directory)}


@router.get('/{asset_id}/file')
@boundary
def file(asset_id: str):
    row = assets.get_asset(asset_id)
    return FileResponse(assets.asset_path(asset_id), filename=row['filename'])
