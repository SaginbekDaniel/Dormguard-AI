"""Copy verified local demo model into a private runtime bundle; never download weights."""

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dormguard.config import Settings
from dormguard.model import load_model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("docs/ai/OFFLINE_VIDEOMAE_MANIFEST.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--noncommercial-rights-confirmed", action="store_true")
    args = parser.parse_args()
    if not args.noncommercial_rights_confirmed:
        parser.error("Explicit permitted noncommercial scope is required")
    if args.output.exists():
        parser.error("Choose a new output directory; existing files are never overwritten")
    model = load_model(
        Settings(
            args.output,
            auth_mode="local",
            demo_mode="offline",
            model_manifest=args.manifest,
            model_rights_confirmed=True,
        )
    )
    data = json.loads(args.manifest.read_text())
    if data.get("format") != "videomae_safetensors_demo":
        parser.error("This packager accepts the reviewed demo VideoMAE manifest only")
    source = (args.manifest.resolve().parent / data["checkpoint_dir"]).resolve()
    args.output.mkdir(mode=0o700, parents=True)
    for name in data["files"]:
        shutil.copyfile(source / name, args.output / name)
    data["checkpoint_dir"] = "."
    (args.output / "manifest.json").write_text(json.dumps(data, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": "PASS",
                "model": model.name,
                "bundle": str(args.output),
                "distribution": "Private noncommercial demo provision; no public publication",
            }
        )
    )


if __name__ == "__main__":
    main()
