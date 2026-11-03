"""Download the AI separation model into models/ (one-off setup).

    python server/fetch_model.py [--model UVR_MDXNET_KARA_2] [--out models/]

The stock UVR export has a fixed 256-frame time axis, which peaks near 1 GB
during inference.  The network only mixes along frequency (MatMul over the
frequency bins, convolutions elsewhere), so the time axis is made variable
here; ktv.separate then feeds 64 frames at a time (~300 MB peak).

Needs the `onnx` package for the patch step only: run it on any machine
(`pip install onnx`) and copy the resulting `<name>.dyn.onnx` to the server.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/"
ROOT = Path(__file__).resolve().parents[1]


def download(name: str, target: Path) -> None:
    if target.exists() and target.stat().st_size > 0:
        print(f"have {target}")
        return
    url = RELEASE + name + ".onnx"
    print(f"download {url}")
    part = target.with_suffix(".part")
    with urllib.request.urlopen(url, timeout=60) as response, open(part, "wb") as out:
        while block := response.read(1 << 20):
            out.write(block)
    part.replace(target)


def make_dynamic(source: Path, target: Path) -> None:
    import onnx

    model = onnx.load(str(source))
    for value in (*model.graph.input, *model.graph.output):
        dims = value.type.tensor_type.shape.dim
        dims[3].ClearField("dim_value")
        dims[3].dim_param = "frames"
    onnx.checker.check_model(model)
    onnx.save(model, str(target))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", default="UVR_MDXNET_KARA_2")
    parser.add_argument("--out", type=Path, default=ROOT / "models")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    stock = args.out / f"{args.model}.onnx"
    final = args.out / f"{args.model}.dyn.onnx"
    download(args.model, stock)
    make_dynamic(stock, final)
    stock.unlink()
    digest = hashlib.sha256(final.read_bytes()).hexdigest()[:16]
    print(f"ready {final} ({final.stat().st_size // 1_000_000} MB, sha256 {digest}…)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
