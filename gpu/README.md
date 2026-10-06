# GPU host on RunPod (RTX 4090): N-ATLAS ASR, N-ATLaS LLM, YarnGPT2 TTS

Three services on one 24 GB card, reached by the backend over RunPod's HTTPS proxy with one shared bearer token.

| Service | Model | Port | Code |
|---|---|---|---|
| LLM | `NCAIR1/N-ATLaS` via vLLM (fp8) | 8001 | `serve_llm.sh` |
| ASR | four `NCAIR1` Whisper fine-tunes | 9001 | `asr_server.py` |
| TTS | `saheedniyi/YarnGPT2` | 9002 | `tts_server.py` |

`scripts/gpu_smoke.py` checks every service on the host: language understanding, speech recognition and speech output.

## Why fp8 on a 4090

About 19 GB with the LLM in fp8 and about 25 GB in bf16, on a card with about 22.5 GB usable. The 4090 has native fp8, so fp8 is the default (`MODE=fp8`, set by `runpod/start.sh`).

| Component | fp8 | bf16 |
|---|---|---|
| N-ATLaS weights (8B) | ~8.5 GB | ~16 GB |
| vLLM activations, CUDA graphs, KV cache | ~5 GB (about 6 concurrent 4k-token sequences) | ~3 GB |
| ASR: 2 Whisper Small (English, Yoruba), fp16, plus CUDA context | ~1.7 GB (~2.7 GB with all four) | ~1.7 GB |
| TTS: YarnGPT2 + WavTokenizer plus CUDA context | ~3 GB | ~3 GB |
| **Total** | **~19 GB, fits** | **~25 GB, does not fit** |

Quantised weights change the model slightly. Compare intent accuracy against bf16 on your labelled calls.
If you later rent a 48 GB card, `MODE=bf16 bash gpu/runpod/start.sh` runs it at full precision.

## Languages

The pilot runs **English and Yoruba**. With `DUAL_ASR=true` (default) the backend sends every caller turn to both speech models in parallel, because callers mix languages in one sentence: ASR load is roughly double per turn, so check `scripts/gpu_smoke.py --wav` against the 0.8 s budget and set `DUAL_ASR=false` if it does not fit. `start.sh` loads only those two speech models (`ASR_LANGUAGES=en,yo`), which saves roughly 1 GB and start-up time (my estimate). Keep it equal to the backend's `ENABLED_LANGUAGES`. To add Hausa or Igbo later, set both to include them, run the accuracy test on real calls first, and get their reply translations.

## Deploy the pod

1. **Storage.** Create an **80 GB network volume** first (it survives pod termination and can be attached to a replacement pod).
   Pick a datacenter that currently lists RTX 4090 availability; a volume is tied to its datacenter, so a full datacenter
   would strand it. If none is available, use the pod's own volume disk instead. Size estimate: three virtualenvs plus
   the models come to roughly 50-60 GB, so 80 GB leaves room.
2. **Pod.** GPU: RTX 4090. Template: a RunPod PyTorch image (Ubuntu 22.04, Python 3.10 or newer). Attach the volume at `/workspace`.
   Container disk: 30 GB. **Expose HTTP ports: 8001, 9001, 9002.** Prefer a datacenter close to Nigeria (Europe is the
   likeliest); measure with the smoke test rather than guessing.
3. **CUDA version.** Current vLLM wheels need a recent NVIDIA driver, and RunPod lets you filter pods by CUDA version.
   I did not verify vLLM's exact minimum, so pick the newest CUDA version offered and let `bootstrap.sh` confirm that torch
   sees the GPU (it fails loudly if not).
4. **Environment variables** (pod settings or template; treat as secrets): `GPU_API_KEY` (generate with `openssl rand -hex 24`)
   and `HF_TOKEN`. Alternatively put them in `/workspace/sofa.env` (see `runpod/sofa.env.example`, `chmod 600`).

## First-time setup (once per volume)

Open the pod's web terminal or SSH in, then get the repo onto the volume (a GitHub repo and `git clone` is easiest, otherwise `scp` a zip):

```bash
cd /workspace && git clone <your-private-repo-url> sofa      # or: unzip sofa.zip -d /workspace/sofa
export HF_TOKEN=hf_...
bash gpu/runpod/bootstrap.sh
```

`bootstrap.sh` (idempotent, so re-run it after fixing anything):

- checks the GPU and free disk;
- builds three virtualenvs under `/workspace/venvs` (vLLM pins its own torch, so LLM, ASR and TTS stay separate) and checks
  each can use CUDA;
- fetches the YarnGPT2 code and WavTokenizer files into `/workspace/yarn`;
- pre-downloads all six models into `/workspace/hf-cache` and prints `FAIL` for any repo your token cannot open
  (accept the `NCAIR1/N-ATLaS` terms on Hugging Face with the token's account first).

If the WavTokenizer `gdown` download fails, the script tells you where to save the file by hand (link on the YarnGPT2 model card).

## Start, restart and stop

```bash
bash gpu/runpod/start.sh            # after every pod start or restart: /workspace persists, the container disk does not
bash gpu/runpod/start.sh status
bash gpu/runpod/start.sh stop
```

`start.sh` loads the secrets, sets offline mode (models come from the volume, not the Hub), starts the LLM first (it claims its
memory slice), then ASR, then TTS, and waits for each `/health`. ASR and TTS warm their models at startup, so the first caller
turn is not the slow one. It ends by printing the `.env` lines for the backend.

The services do not restart themselves after a pod restart. Run `start.sh` again. Making it automatic via the template's container
start command is possible, but I have not verified the syntax for your image, so I have not scripted it.

## Verify before touching the phone side

From your own machine (Windows is fine) with the URLs `start.sh` printed:

```bash
export LLM_URL=https://POD_ID-8001.proxy.runpod.net ASR_URL=https://POD_ID-9001.proxy.runpod.net TTS_URL=https://POD_ID-9002.proxy.runpod.net GPU_API_KEY=...
python -m scripts.gpu_smoke --wav some_call.wav --lang yo
```

It uses the backend's own clients, so a pass means the production code path works, including the proxy. It checks eight typical sentences
for schema-valid intent JSON, the rerank choice, **the gateway's two per-turn calls (UNDERSTAND and WORDING, in English, Yoruba and Pidgin;
the Yoruba and Pidgin lines are informational, so read them)**, TTS in all four languages (writes `gpu_smoke_out/tts_*.wav`: **listen to them**),
and, with `--wav`, ASR on a real recording. It then reports median latency over repeats against the budget
(ASR 0.8 s, LLM 1.0 s, TTS 1.2 s), which is the real test of whether the proxy and distance are acceptable.

Then run `python -m scripts.asr_gate` on real phone recordings for the Day-1 accuracy numbers.

## Connect the backend

```
LLM_URL=https://POD_ID-8001.proxy.runpod.net
ASR_URL=https://POD_ID-9001.proxy.runpod.net
TTS_URL=https://POD_ID-9002.proxy.runpod.net
GPU_API_KEY=<same key>
LLM_STRUCTURED_MODE=modern     # vLLM >= 0.12
```

The pod ID is in the URL, so a **replacement pod has a new ID and the backend URLs must be updated**. A stopped and restarted
pod keeps its ID.

## RunPod behaviours to know

- **HTTPS is provided** by the proxy (`https://POD_ID-PORT.proxy.runpod.net`), so no reverse proxy is needed. The docs say services must bind
  `0.0.0.0` (they do) and that Cloudflare closes any request that takes over **100 seconds** (nothing here comes close).
- **There is no IP allow-listing on the proxy.** Anyone who guesses the URL can reach it, so the bearer token is the only lock:
  keep it long and random, never commit it, and rotate it if it leaks. `/docs` is disabled on ASR and TTS; vLLM's `--api-key`
  protects `/v1/*` but leaves `/health` and `/metrics` open, which is harmless.
- **Container disk is wiped on stop; `/workspace` is not.** Everything in this guide lives under `/workspace` for that reason.
- **Stop the pod when you are not testing** to stop GPU charges.

## Things I could not confirm

- **N-ATLaS under vLLM:** the model card gives no vLLM command and I could not test that the model loads with these settings.
  The log (`/workspace/logs/llm.log`) will say why if it does not (token, gated repo, memory).
- **Whether vLLM's exact CUDA and driver requirement matches your pod:** `bootstrap.sh` checks that torch can see the GPU, which catches
  the common failure, but not every incompatibility.
- **Structured output API:** vLLM 0.12 removed `guided_json` and `guided_choice`. The backend sends `response_format` and
  `structured_outputs.choice`; `gpu_smoke` shows the error if your vLLM rejects them (set `LLM_STRUCTURED_MODE=legacy` for older versions).
- **YarnGPT2:** the voice names and the WavTokenizer download link are taken from the model card and are only checked on first use.
  Audio quality in Yoruba, Hausa and Igbo, and on a phone line, is unknown until you listen.
- **`/proc/1/environ` secret loading in `start.sh`:** written from my understanding of how RunPod passes environment variables;
  if `start.sh` says the key is unset, use `/workspace/sofa.env`.
- **Number words:** English numbers are spelled out before synthesis (`textnorm.py`); Yoruba, Hausa and Igbo digits are left
  as they are. The spec's open decision on how prices are spoken there still needs an answer.
- **Concurrency:** TTS runs one generation at a time and vLLM is capped at 4 sequences. That suits a pilot; a busy line queues
  on novel sentences (repeated phrases come from the backend's audio cache).
