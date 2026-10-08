"""Request Windows folder opening after the caller validates its ownership."""
import os
from pathlib import Path
import subprocess


def open_registered_directory(directory):
    target=Path(directory).resolve()
    if not target.is_dir():raise ValueError('结果目录不存在')
    opener=getattr(os,'startfile',None)
    if opener is None:raise RuntimeError('当前系统不支持打开本地文件夹')
    try:
        opener(str(target))
    except OSError as error:
        if not isinstance(error,PermissionError) and getattr(error,'winerror',None)!=5:raise
        # Some restricted service contexts cannot delegate ShellExecute's
        # directory verb. Send the already validated path directly to Explorer.
        explorer=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'explorer.exe'
        if not explorer.is_absolute():raise RuntimeError('Windows 资源管理器路径无效') from error
        subprocess.Popen([str(explorer),str(target)],shell=False,
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    # Launch acceptance is a request, not proof that a window became visible.
