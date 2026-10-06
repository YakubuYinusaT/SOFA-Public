#!/usr/bin/env bash
# Serve NCAIR1/N-ATLaS with vLLM (OpenAI-compatible API on :8001). The backend only ever calls
# /v1/chat/completions, with a JSON-schema response_format for turns and structured_outputs.choice
# for product rerank (vLLM >= 0.12; set LLM_STRUCTURED_MODE=legacy in the backend for older vLLM).
#
#   GPU_API_KEY=... HF_TOKEN=... ./gpu/serve_llm.sh
#
# Start this FIRST on a shared card: vLLM claims its memory slice at startup, and ASR/TTS then
# fit into what is left (see gpu/README.md "Memory budget").
set -euo pipefail

: "${GPU_API_KEY:?set GPU_API_KEY (the backend sends it as a Bearer token)}"
: "${HF_TOKEN:?set HF_TOKEN (the NCAIR1/N-ATLaS repo is gated: accept its terms on Hugging Face first)}"
export HF_TOKEN

MODE="${MODE:-fp8}"          # fp8: 8B weights ~8.5 GB, fits with ASR+TTS on one 24 GB card.
                             # bf16: 8B weights ~16 GB. Full precision, but leaves almost no room
                             #       for ASR+TTS on 24 GB: use a 40/48/80 GB card, or a second GPU.
PORT="${PORT:-8001}"
MAX_LEN="${MAX_LEN:-4096}"   # prompts are ~1.5k tokens (30 products + rules + last 2 turns)

if [[ "$MODE" == "bf16" ]]; then
  UTIL="${GPU_MEM_UTIL:-0.85}"
  EXTRA=()
else
  UTIL="${GPU_MEM_UTIL:-0.60}"
  EXTRA=(--quantization fp8)
fi

echo "N-ATLaS via vLLM: mode=$MODE gpu-memory-utilization=$UTIL max-model-len=$MAX_LEN port=$PORT"

# --generation-config vllm: ignore the repo's generation_config.json so no repetition penalty or
#   top-p sneaks into JSON extraction; every request sets temperature 0.1 itself.
# --max-num-seqs 4: a few concurrent calls is the pilot; more just eats KV cache.
exec vllm serve NCAIR1/N-ATLaS \
  --served-model-name NCAIR1/N-ATLaS \
  --host 0.0.0.0 --port "$PORT" \
  --api-key "$GPU_API_KEY" \
  --dtype bfloat16 \
  --max-model-len "$MAX_LEN" \
  --max-num-seqs 4 \
  --max-num-batched-tokens 2048 \
  --gpu-memory-utilization "$UTIL" \
  --generation-config vllm \
  "${EXTRA[@]}"
