#!/usr/bin/env bash
# Start (or restart) all three services on a RunPod pod. Run this after every pod start/restart:
# the container disk is wiped on stop, but /workspace (repo, venvs, models) is not.
#
#   bash gpu/runpod/start.sh            # start   (also: stop | status)
#
# Secrets: GPU_API_KEY and HF_TOKEN are read, in order, from the current environment,
# /workspace/sofa.env (KEY=value lines, chmod 600), then the pod's own environment (/proc/1/environ,
# which is where RunPod puts variables set in the template or pod settings; SSH sessions don't inherit them).
set -euo pipefail

SOFA_HOME="${SOFA_HOME:-/workspace/sofa}"
cd "$SOFA_HOME"

load() {  # NAME
  [[ -n "${!1:-}" ]] && return 0
  if [[ -f /workspace/sofa.env ]]; then
    local v; v=$(grep -E "^$1=" /workspace/sofa.env | head -1 | cut -d= -f2- || true)
    [[ -n "$v" ]] && { export "$1=$v"; return 0; }
  fi
  if [[ -r /proc/1/environ ]]; then
    local v; v=$(tr '\0' '\n' </proc/1/environ | grep -E "^$1=" | head -1 | cut -d= -f2- || true)
    [[ -n "$v" ]] && { export "$1=$v"; return 0; }
  fi
  return 0
}
load GPU_API_KEY
load HF_TOKEN
if [[ "${1:-start}" == "start" ]]; then
  : "${GPU_API_KEY:?GPU_API_KEY is not set. This host is public. Generate one with: openssl rand -hex 24 and put it in /workspace/sofa.env or the pod environment variables}"
  : "${HF_TOKEN:?HF_TOKEN is not set. Put it in the environment, /workspace/sofa.env or the pod environment variables}"
fi

export HF_HOME=/workspace/hf-cache
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"   # models are already on the volume; do not depend on the Hub at start time
export PYTHONPATH="/workspace/yarn:${PYTHONPATH:-}"
export WAVTOKENIZER_CKPT="${WAVTOKENIZER_CKPT:-/workspace/yarn/wavtokenizer_large_speech_320_24k.ckpt}"
export WAVTOKENIZER_CONFIG="${WAVTOKENIZER_CONFIG:-/workspace/yarn/wavtokenizer_mediumdata_frame75_3s_nq1_code4096_dim512_kmeans200_attn.yaml}"
export VENV_LLM=/workspace/venvs/llm VENV_ASR=/workspace/venvs/asr VENV_TTS=/workspace/venvs/tts
export LOGDIR="${LOGDIR:-/workspace/logs}"
export ASR_LANGUAGES="${ASR_LANGUAGES:-en,yo}"   # pilot languages: only these speech models are loaded (keep equal to the backend ENABLED_LANGUAGES)
export MODE="${MODE:-fp8}"   # RTX 4090 (24 GB): fp8 is the only mode that fits LLM + ASR + TTS. See gpu/README.md

# The RunPod pod image runs nginx on several ports as placeholder pages, and one of them is 8001, the language model's port. Free it,
# otherwise vLLM stops with "Address already in use" while the health check still sees nginx answering. (Jupyter and the web
# terminal do not use nginx, so a connection to the pod is not affected.)
# (The listing is captured first: "ss | grep -q" can end the pipe early, and with pipefail that made this check fail on the second pod.)
if [[ "${1:-start}" == "start" ]]; then
  listening=$(ss -ltnp 2>/dev/null || true)
  if grep -qE ':8001 .*nginx' <<<"$listening"; then
    echo "stopping the pod's nginx placeholder server (it holds port 8001)..."
    pkill -9 nginx 2>/dev/null || true
    sleep 1
  fi
fi

./gpu/run_all.sh "${1:-start}"

if [[ "${1:-start}" == "start" && -n "${RUNPOD_POD_ID:-}" ]]; then
  cat <<EOF

Backend .env (RunPod's HTTPS proxy; expose HTTP ports 8001, 9001 and 9002 in the pod settings first):
  LLM_URL=https://${RUNPOD_POD_ID}-8001.proxy.runpod.net
  ASR_URL=https://${RUNPOD_POD_ID}-9001.proxy.runpod.net
  TTS_URL=https://${RUNPOD_POD_ID}-9002.proxy.runpod.net
  GPU_API_KEY=<the same key>
EOF
fi
