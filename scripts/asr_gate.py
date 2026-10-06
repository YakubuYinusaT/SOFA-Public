"""Day-1 gate: word error rate of N-ATLAS ASR on real phone recordings, per language.

Put recordings in a folder as  name.wav  with the true words in  name.txt.  Then:

    python -m scripts.asr_gate --asr-url http://GPU_HOST:9000 --lang yo --dir recordings/yo
    python -m scripts.asr_gate --asr-url http://GPU_HOST:9000 --lang ha --dir recordings/ha

Run once per language and choose the pilot language from the numbers. Send the audio as recorded
(8 kHz phone audio); this script resamples it to 16 kHz mono first, like the backend does.
"""

import argparse
import json
import statistics
from pathlib import Path

import httpx

from sofa.audio import to_16k_mono
from sofa.textutil import wer


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asr-url", required=True)
    ap.add_argument("--lang", required=True, choices=["en", "yo", "ha", "ig"])
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()

    rows = []
    for wav in sorted(Path(args.dir).glob("*.wav")):
        ref_path = wav.with_suffix(".txt")
        if not ref_path.exists():
            continue
        audio = to_16k_mono(wav.read_bytes())
        resp = httpx.post(f"{args.asr_url.rstrip('/')}/asr", files={"audio": (wav.name, audio, "audio/wav")},
                          data={"language": args.lang}, timeout=120)
        resp.raise_for_status()
        out = resp.json()
        ref = ref_path.read_text(encoding="utf-8")
        rows.append({"file": wav.name, "ref": ref.strip(), "hyp": out["text"], "confidence": out["confidence"],
                     "wer": round(wer(ref, out["text"]), 3)})
        print(f"{wav.name}: WER {rows[-1]['wer']:.2f}  conf {out['confidence']:.2f}\n  ref: {ref.strip()}\n  hyp: {out['text']}")
    if not rows:
        raise SystemExit("no .wav/.txt pairs found")
    summary = {"language": args.lang, "n": len(rows), "mean_wer": round(statistics.mean(r["wer"] for r in rows), 3),
               "median_wer": round(statistics.median(r["wer"] for r in rows), 3)}
    out_name = f"asr_gate_{Path(args.dir).name}_{args.lang}.json"  # folder name keeps mixed runs from overwriting
    Path(out_name).write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nSUMMARY", summary)


if __name__ == "__main__":
    main()
