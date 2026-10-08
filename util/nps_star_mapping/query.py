"""Query a saved calibration without invoking V32 or modifying files."""
import argparse
import json
from pathlib import Path
from mapping import inverse

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mapping', type=Path, required=True)
    parser.add_argument('--nps', type=float, nargs='+', required=True)
    args = parser.parse_args()
    model = json.loads(args.mapping.read_text(encoding='utf-8'))
    print(json.dumps([inverse(model, value) for value in args.nps], ensure_ascii=False, indent=2))
