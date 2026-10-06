#!/usr/bin/env bash
# ONE-TIME setup of a RunPod pod. Everything lands under /workspace, which survives a pod stop or
# restart (but not termination), so this only needs to run once per volume.
#
#   export HF_TOKEN=hf_...            # read token from the account that accepted the N-ATLaS terms
#   bash gpu/runpod/bootstrap.sh
#
# Idempotent: re-running skips what is already there. Takes a while (about 45+ GB of downloads).
set -uo pipefail

SOFA_HOME="${SOFA_HOME:-/workspace/sofa}"
VENVS="${VENVS:-/workspace/venvs}"
YARN="${YARN:-/workspace/yarn}"
# Always the same folder start.sh uses. (A RunPod pod can arrive with HF_HOME already set to another folder; keeping that one made
# the models land where start.sh does not look.)
export HF_HOME=/workspace/hf-cache
# The token can come from the environment or from /workspace/sofa.env (a line HF_TOKEN=hf_...), which is easier
# to set up in RunPod's web terminal.
if [[ -z "${HF_TOKEN:-}" && -f /workspace/sofa.env ]]; then
  HF_TOKEN=$(grep -E '^HF_TOKEN=' /workspace/sofa.env | head -1 | cut -d= -f2- || true)
fi
: "${HF_TOKEN:?HF_TOKEN not found. Put a line HF_TOKEN=hf_... in /workspace/sofa.env (accept the NCAIR1/N-ATLaS terms on Hugging Face with that account first)}"
export HF_TOKEN

fail=0
step() { printf '\n== %s\n' "$*"; }

step "GPU and disk"
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv || { echo "no GPU visible"; exit 1; }
avail_gb=$(df -BG --output=avail /workspace | tail -1 | tr -dc '0-9')
echo "free space on /workspace: ${avail_gb} GB"
[[ "${avail_gb:-0}" -ge 70 ]] || echo "WARNING: under 70 GB free. Three venvs plus models need roughly 50-60 GB; resize the volume."

mkdir -p "$VENVS" "$HF_HOME" "$YARN"
cd "$SOFA_HOME" || { echo "put the repo at $SOFA_HOME first (git clone or upload)"; exit 1; }

make_venv() {  # name requirements-file
  local dir="$VENVS/$1"
  step "venv $1"
  [[ -x "$dir/bin/python" ]] || python3 -m venv "$dir" || { fail=1; return; }
  "$dir/bin/pip" install --upgrade pip >/dev/null
  "$dir/bin/pip" install -r "$2" || { echo "pip install failed for $1"; fail=1; return; }
  # A CPU-only torch would start fine and then crawl: fail here instead.
  "$dir/bin/python" -c "import torch,sys; ok=torch.cuda.is_available(); print('torch',torch.__version__,'cuda',ok); sys.exit(0 if ok else 1)" \
    || { echo "torch cannot see the GPU in venv $1 (driver too old for this torch build? filter pods by a newer CUDA version)"; fail=1; }
}
make_venv llm gpu/requirements-llm.txt
make_venv asr gpu/requirements-asr.txt
make_venv tts gpu/requirements-tts.txt
"$VENVS/tts/bin/pip" install gdown >/dev/null 2>&1 || true

step "YarnGPT2 code and WavTokenizer files"
[[ -d "$YARN/yarngpt" ]] || git clone https://github.com/saheedniyi02/yarngpt.git "$YARN/yarngpt" || fail=1
CFG=wavtokenizer_mediumdata_frame75_3s_nq1_code4096_dim512_kmeans200_attn.yaml
[[ -f "$YARN/$CFG" ]] || wget -q -O "$YARN/$CFG" "https://huggingface.co/novateur/WavTokenizer-medium-speech-75token/resolve/main/$CFG" || fail=1
CKPT=wavtokenizer_large_speech_320_24k.ckpt
if [[ ! -f "$YARN/$CKPT" ]]; then
  ( cd "$YARN" && "$VENVS/tts/bin/gdown" 1-ASeEkrn4HY49yZWHTASgfGFNXdVnLTt -O "$CKPT" ) \
    || { echo "gdown failed: get the WavTokenizer .ckpt from the YarnGPT2 model card and save it as $YARN/$CKPT"; fail=1; }
fi

step "Pre-download models (so the first start is fast and access problems show up now)"
"$VENVS/llm/bin/python" - <<'PY' || fail=1
import os, sys
from huggingface_hub import snapshot_download
repos = ["NCAIR1/N-ATLaS", "NCAIR1/NigerianAccentedEnglish", "NCAIR1/Yoruba-ASR",
         "NCAIR1/Hausa-ASR", "NCAIR1/Igbo-ASR", "saheedniyi/YarnGPT2"]
bad = 0
for repo in repos:
    try:
        path = snapshot_download(repo, token=os.environ["HF_TOKEN"])
        print("ok  ", repo, "->", path)
    except Exception as exc:  # gated repo without accepted terms, wrong token, name changed
        bad += 1
        print("FAIL", repo, "::", type(exc).__name__, str(exc)[:160])
sys.exit(1 if bad else 0)
PY

step "Result"
if [[ "$fail" -eq 0 ]]; then
  echo "bootstrap complete. Next:  bash gpu/runpod/start.sh"
else
  echo "bootstrap finished with problems (see FAIL / WARNING lines above). Fix them and re-run: it resumes."
  exit 1
fi
