# Model configuration and downloads

This directory contains model configuration and source versions. Setup downloads the required weights to your computer.

| Component | Pinned source | Local setup |
| --- | --- | --- |
| MuG Diffusion | [Checkpoint](https://huggingface.co/ayousanz/Mug-Diffusion-model), verified by SHA-256 | `tools/download_file.py`, then `tools/validate_mug_install.py` |
| V32 mania and timing | [Revision 74f2258](https://huggingface.co/OliBomby/Mapperatorinator-v32/tree/74f22583400d259bf424819e11027c17933efe54) | `tools/download_mapperatorinator.py` and `--base`, then `tools/validate_v32_install.py` |
| Beat This | [Source b95c8ab](https://github.com/CPJKU/beat_this/tree/b95c8ab0c58c2d9fcfd40508ae8dffbc05ac4f5c), final0/1/2 checkpoints | `tools/setup_beat_analysis.py` |
| Demucs | htdemucs / optional htdemucs_ft, 4.0.1 | `tools/setup_separation.ps1` |
| Kim Mel-Band RoFormer | [MSST source e247dfe](https://github.com/ZFTurbo/Music-Source-Separation-Training/tree/e247dfe4abc1f17c69dff719207fe045dc04413a) and [weight revision ac9b061](https://huggingface.co/KimberleyJSN/melbandroformer/tree/ac9b0614ab3cd7f77219e18ba494dfd93956c348) | `tools/setup_roformer.ps1` |

The main, V32, beat-analysis, Demucs and RoFormer environments are isolated. Their validated package versions are in `requirements-lock.txt` and `runtime/*requirements-lock.txt`. Setup validates the selected models and saves a local readiness record.

Weight hashes:

| File | SHA-256 |
| --- | --- |
| MuG `model.ckpt` | `af6ab91337d0ef6b518367082ac3f849448c6daaa01fd987678fb25ea44ca184` |
| V32 mania `model.safetensors` | `3d5d1c2a01ad462bcc99bafaaf44174bc47be099724574b17cd68016be5a863f` |
| V32 timing `model.safetensors` | `a79fd39a72f2ae814b397f8b4c991fac0b5faa9fbaca3f8f0df91289b23b9707` |
| Beat This `final1.ckpt` | `365b553f43750717c907f32fbc42910f3b264d616654583df6e44472c93ead80` |
| Kim `MelBandRoformer.ckpt` | `87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e` |

Beat This final1 supplies analysis and BPM buckets for generation conditions. Automatic V32 timing-reference injection is disabled. See [generation conditions](../docs/chart-quality.md#生成条件) for details.

MuG's upstream restrictions include non-commercial use of its weights and generated charts. Component licenses and model terms are linked above. Input music and artwork remain subject to their owners' rights.

## 手动安装与可选组件

以下命令均在项目根目录运行。

主环境与 MuG：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe tools\download_file.py https://huggingface.co/ayousanz/Mug-Diffusion-model/resolve/main/model.ckpt models\mug-diffusion\v1.0.0\model.ckpt --workers 4 --sha256 af6ab91337d0ef6b518367082ac3f849448c6daaa01fd987678fb25ea44ca184
.\.venv\Scripts\python.exe tools\validate_mug_install.py
```

V32：

```powershell
py -3.12 -m venv runtime\mapperatorinator-venv
.\runtime\mapperatorinator-venv\Scripts\python.exe -m pip install -r runtime\mapperatorinator-requirements-lock.txt
.\.venv\Scripts\python.exe tools\download_mapperatorinator.py
.\.venv\Scripts\python.exe tools\download_mapperatorinator.py --base
.\runtime\mapperatorinator-venv\Scripts\python.exe tools\validate_v32_install.py
.\.venv\Scripts\python.exe tools\setup_beat_analysis.py
```

可选声部分离，分别使用独立环境：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools\setup_separation.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File tools\setup_roformer.ps1
```

在线音乐下载使用 yt-dlp。**`一键连接YouTube.bat`** 使用专用 Edge 登录窗口，将登录 Cookie 保存在项目的 `private/` 目录。此可选入口需要 Node.js 与 Microsoft Edge；普通公开音源先尝试匿名下载。也可参考 `runtime/music-source-settings.example.json` 配置本地 Cookie 文件。
