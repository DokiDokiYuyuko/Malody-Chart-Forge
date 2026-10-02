"""Load the pinned MuG checkpoint once to verify local files and model configuration."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from malody_studio.engine import Engine


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    engine = Engine()
    engine.load(lambda message, percent: print(f"{percent}% {message}", flush=True))
    print(f"MuG 模型加载通过：{engine.device}", flush=True)
    engine.unload()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"MuG 模型加载失败：{error}", flush=True)
        raise SystemExit(1)
