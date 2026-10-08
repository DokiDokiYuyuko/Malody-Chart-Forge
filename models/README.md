# Model configuration and downloads

This directory contains inference configuration, public source identities and the Beat This selection policy. Weight files and machine-specific readiness records are downloaded/generated locally and excluded from Git.

| Component | Pinned source | Local setup |
| --- | --- | --- |
| MuG Diffusion | [Checkpoint](https://huggingface.co/ayousanz/Mug-Diffusion-model), verified by SHA-256 | `tools/download_file.py`, then `tools/validate_mug_install.py` |
| V32 mania and timing | [Revision 74f2258](https://huggingface.co/OliBomby/Mapperatorinator-v32/tree/74f22583400d259bf424819e11027c17933efe54) | `tools/download_mapperatorinator.py` and `--base`, then `tools/validate_v32_install.py` |
| Beat This | [Source b95c8ab](https://github.com/CPJKU/beat_this/tree/b95c8ab0c58c2d9fcfd40508ae8dffbc05ac4f5c), final0/1/2 checkpoints | `tools/setup_beat_analysis.py` |
| Demucs | htdemucs / optional htdemucs_ft, 4.0.1 | `tools/setup_separation.ps1` |
| Kim Mel-Band RoFormer | [MSST source e247dfe](https://github.com/ZFTurbo/Music-Source-Separation-Training/tree/e247dfe4abc1f17c69dff719207fe045dc04413a) and [weight revision ac9b061](https://huggingface.co/KimberleyJSN/melbandroformer/tree/ac9b0614ab3cd7f77219e18ba494dfd93956c348) | `tools/setup_roformer.ps1` |

The main, V32, beat-analysis, Demucs and RoFormer environments are isolated. Their validated package versions are in `requirements-lock.txt` and `runtime/*requirements-lock.txt`. Setup writes a local readiness record only after its checks succeed; the repository does not claim that a fresh checkout already has working weights.

Key public weight hashes:

| File | SHA-256 |
| --- | --- |
| MuG `model.ckpt` | `af6ab91337d0ef6b518367082ac3f849448c6daaa01fd987678fb25ea44ca184` |
| V32 mania `model.safetensors` | `3d5d1c2a01ad462bcc99bafaaf44174bc47be099724574b17cd68016be5a863f` |
| V32 timing `model.safetensors` | `a79fd39a72f2ae814b397f8b4c991fac0b5faa9fbaca3f8f0df91289b23b9707` |
| Beat This `final1.ckpt` | `365b553f43750717c907f32fbc42910f3b264d616654583df6e44472c93ead80` |
| Kim `MelBandRoformer.ckpt` | `87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e` |

Beat This final1 is selected for analysis and BPM buckets. Automatic strong V32 timing references are not approved by this policy. Model output, NPS compliance, listening and game validation remain separate checks.

MuG's upstream restrictions include non-commercial use of its weights and generated charts. Check each component's original license and model terms; they grant no rights to input music or artwork. See the root README for installation and use.
