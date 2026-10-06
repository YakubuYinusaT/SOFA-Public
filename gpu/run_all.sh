#!/usr/bin/env bash
# Start the three GPU services on one host, LLM first (it claims its memory slice up front).
#
#   GPU_API_KEY=... HF_TOKEN=... ./gpu/run_all.sh start     # start (default)
#   ./gpu/run_all.sh stop
#   ./gpu/run_all.sh status
#
# Each service has its own virtualenv because vLLM pins its own torch/transformers versions
# (see gpu/README.md). Override the paths with VENV_LLM / VENV_ASR / VENV_TTS.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV_LLM="${VENV_LLM:-/opt/venv-llm}"
VENV_ASR="${VENV_ASR:-/opt/venv-asr}"
VENV_TTS="${VENV_TTS:-/opt/venv-tts}"
LOGDIR="${LOGDIR:-./logs}"
PIDDIR="$LOGDIR/pids"
mkdir -p "$LOGDIR" "$PIDDIR"

cmd="${1:-start}"

wait_for() {  # url name timeout_seconds
  for _ in $(seq 1 "$3"); do
    curl -fs "$1" >/dev/null 2>&1 && { echo "  $2 is up"; return 0; }
    sleep 2
  done
  echo "  $2 did not come up: see $LOGDIR/$2.log" >&2
  return 1
}

case "$cmd" in
  start)
    : "${GPU_API_KEY:?set GPU_API_KEY}"
    : "${HF_TOKEN:?set HF_TOKEN}"
    export GPU_API_KEY HF_TOKEN
    # A service that already answers is left alone, so running this again only starts what is missing. The first start of the
    # language model is slow (about 4 minutes to load and prepare), hence the long wait.
    up() { curl -fs "$1" >/dev/null 2>&1; }
    if up http://127.0.0.1:8001/health; then echo "llm is already up"; else
      echo "starting llm (first: it claims its GPU memory)..."
      ( set +u; source "$VENV_LLM/bin/activate"; set -u; nohup ./gpu/serve_llm.sh >"$LOGDIR/llm.log" 2>&1 & echo $! >"$PIDDIR/llm" )
      wait_for http://127.0.0.1:8001/health llm 450
    fi
    if up http://127.0.0.1:9001/health; then echo "asr is already up"; else
      echo "starting asr..."
      ( set +u; source "$VENV_ASR/bin/activate"; set -u; nohup uvicorn gpu.asr_server:app --host 0.0.0.0 --port 9001 >"$LOGDIR/asr.log" 2>&1 & echo $! >"$PIDDIR/asr" )
      wait_for http://127.0.0.1:9001/health asr 240
    fi
    if up http://127.0.0.1:9002/health; then echo "tts is already up"; else
      echo "starting tts..."
      ( set +u; source "$VENV_TTS/bin/activate"; set -u; nohup uvicorn gpu.tts_server:app_factory --factory --host 0.0.0.0 --port 9002 >"$LOGDIR/tts.log" 2>&1 & echo $! >"$PIDDIR/tts" )
      wait_for http://127.0.0.1:9002/health tts 240
    fi
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv || true
    ;;
  stop)
    for s in tts asr llm; do
      [[ -f "$PIDDIR/$s" ]] && { kill "$(cat "$PIDDIR/$s")" 2>/dev/null || true; rm -f "$PIDDIR/$s"; echo "stopped $s"; }
    done
    # vLLM runs its engine in a child process that can outlive the parent and keep the GPU memory
    pkill -f "vllm serve" 2>/dev/null || true
    pkill -f "gpu.asr_server|gpu.tts_server" 2>/dev/null || true
    sleep 3
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv || true
    ;;
  status)
    for pair in "llm 8001" "asr 9001" "tts 9002"; do
      set -- $pair
      curl -fs "http://127.0.0.1:$2/health" >/dev/null 2>&1 && echo "$1: up" || echo "$1: DOWN"
    done
    nvidia-smi --query-gpu=memory.used,memory.total --format=csv || true
    ;;
  *) echo "usage: $0 start|stop|status" >&2; exit 2 ;;
esac
