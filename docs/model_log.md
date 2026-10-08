# Model log: what we measured, what we changed, what it did

A running record of how the models behave in SOFA, every fix made, and the measured effect. It is kept up to date as work goes on, and it is the source for the technical evidence in submissions. Each entry says what was seen, what was done, and the result. Open items are at the end.

The models: **N-ATLaS** (language understanding and wording), **N-ATLaS Whisper** (speech recognition, English and Yoruba), **YarnGPT2** (speech output). They run on one 24 GB GPU host. Measurements below were taken on an NVIDIA L4 unless stated; an RTX 4090 was used earlier.

## 1. Language model (N-ATLaS)

| # | Finding | Change | Effect |
|---|---|---|---|
| 1.1 | The front desk call (language, service, tool, amount, yes/no in one structured answer) was tested on real sentences, including Yoruba and Pidgin. | None needed. | 7 of 7 English checks correct. Yoruba and Pidgin sentences were read correctly in all 5 trials (for example "Jowo, ranse egberun meta si mama" gave a transfer of 3,000 to Mama). Median time 0.85 s on the 4090, about 2.0 to 2.6 s on the L4. |
| 1.2 | The shop's model call was cut off mid-answer: it had a default limit of 200 tokens, shorter than a full answer, so the JSON ended in the middle of a sentence and the turn was treated as unknown. 5 of 8 shop checks failed this way. | The shop turn now has room for 600 tokens, and the gateway's extraction calls for 400 (`TURN_MAX_TOKENS`, `EXTRACT_MAX_TOKENS`). Covered by new tests. | No answer is cut off any more. Understanding of the remaining checks is measured in 1.3. |
| 1.3 | With answers complete, the shop's single-call format still names the wrong intent in 5 of 8 checks (for example "Where is my order?" is read as a request for advice; "Let me talk to the owner" as a price question). The front-desk format, which is flatter and shorter, gets 7 of 7. | Open: move the shop turn to the same compact format as the front desk. | Expected to bring shop understanding to the front desk's level, and to cut its median time from about 5 s toward 2 s. |
| 1.4 | Model-written reply wording is checked before it is spoken (every placeholder kept, no invented number). | None. | The check refused one English wording in the test and the fixed template was spoken instead, as designed. Yoruba and mixed wording passed. |
| 1.5 | Delay per turn on the L4: understanding about 2.4 s, wording about 1.2 s, against a budget of 1 s each. | Open: a faster card for the language model (the 4090 measured 0.85 s), or a smaller output format. | Not yet changed. |
| 1.6 | A whole English call on the real model, from the greeting to the placed order, broke after the advice answer: the caller's next line "I want two bags of NPK fertilizer" was treated as a "no" to the product offer, so the order was lost. The earlier stand-in model never produced this. | The shop dialogue now treats a fresh order (place or modify, with items) as a new request, not a refusal (`advice_offer` in `resolve_pending`). A test fails without the fix and passes with it. | The same call now runs end to end: name, price, advice, order, address, read-back, confirmation, and the account number sent by SMS. |
| 1.7 | In the same call the real model reads "that is all" (after "Anything else?") as unknown, and on a later run it also missed a plain "yes" to the read-back, so the order was not placed. | A plain yes, no or "that is all" given to the question just asked is now decided by SOFA itself, before the model is asked (`plain_answer`). Covered by tests that fail if the model is asked. | "That is all" goes straight to "Where should we deliver?", and "yes" places the order. One model call saved on each. |

## 2. Speech output (YarnGPT2)

| # | Finding | Change | Effect |
|---|---|---|---|
| 2.1 | Speech was generated at about 35 tokens a second. The voice needs 75 tokens for each second of speech, so a sentence took about twice as long to make as to say (4.9 s of speech took 15 s). This held on both the 4090 and the L4, so it comes from per-step overhead, not the card. | The voice engine uses a fixed-size (static) cache so PyTorch can compile the one-token step. If a host cannot do it, the plain method is used. | 68 tokens a second (35 before): 6.2 s of speech made in 8.4 s, about 2.2 times faster. It costs a one-time compile of about 70 s at startup. Compiling the step by hand on top adds nothing. Half precision was already in use. |
| 2.2 | The model has no speed control and speaks fast. | A pitch-preserving slow-down (`gpu/timestretch.py`, setting `TTS_SPEED`, default in the start script 0.92), also available per request as `speed`. | Speech is about 8% longer at the same pitch. Tested on tones: length changes by the requested factor and the pitch stays within 12 Hz. |
| 2.3 | Voices: the default `idera` was compared with `emma`, `jude`, `osagie`, `tayo`, `joke`, `regina`, `remi`, `umar`. Two were not usable in the test (`zainab`: the connection dropped; `enitan`: not installed). | Samples were produced for a listening choice. | `idera` stays the default. Some voices produce much shorter audio than others for the same sentence (2.4 s against 6.2 s), which suggests clipped output; to be checked by ear. |
| 2.3a | Voice ranking by how well the speech recogniser reads each voice back (5 phrases, speed 0.85, 16 kHz). | Run for every voice. | jude 64%, osagie 62%, emma 55%, umar 42%, remi 39%, idera 38%, joke 30%, tayo 20%, regina 1%. The default `idera` ranks in the lower half on clarity; the choice is confirmed by ear. |
| 2.4 | Very short sentences come out rushed or swallowed: "Where should we deliver?" is 0.95 s long and "Thank you." is 0.4 s. A caller heard only "deliver". A longer sentence ("Okay. Where should we deliver the order to?") is 2.5 s but starts after 1.0 s of quiet. | Measured with `scripts/voice_check.py`: the voice speaks each phrase and the speech recogniser writes down what it heard. | With the default voice, 22% of the words came back over 16 phrases. Plain sentences of 6 words or more are read back well (for example "How can I help you today?" 100%, the long greeting 76% to 81%), while one- and two-word replies are not ("Done.", "Yes.", "Okay." 0%). A leading "Okay." also damages the words after it. The first word of many sentences is distorted. |
| 2.4a | Does slowing help? | The same check at speed 0.65. | 36% of words (22% before); "What would you like today?" became 100%. Slowing helps but does not fix short replies. |
| 2.4b | Does full quality help (16 kHz, no stretch), or longer wording? | Both tried. | Longer wording alone: 28%. Full quality alone: 29%. Neither fixes it, so the weakness is in the voice model on short sentences and on onsets. Saying the phrase twice and keeping the second copy did not help either. |
| 2.4c | The connection to the GPU host: Python's default TLS 1.3 failed 7 of 8 times through the host's proxy on the test laptop; TLS 1.2 worked 8 of 8. | SOFA's own clients already use TLS 1.2 with retries (`sofa/clients/http.py`); the check script now does too. | Stable. |
| 2.5 | Replies wait for the voice: with the real model a spoken reply needs about 40 s from the end of the caller's turn, and SOFA plays 4 to 5 holding messages meanwhile. | Holding messages, call-back and a call time limit exist. The voice speed-up in 2.1 shortens the wait. | To be re-measured end to end with the full stack. |

| 2.6 | The voice was changed to `jude` with punctuation pauses (the model drops commas and full stops, so SOFA splits the text and inserts quiet gaps itself). | Same 16-phrase read-back check, speed 0.92. | 31% of words over 16 phrases (22% with the old default voice). Long wordings read back at 58% to 59%; one- and two-word replies are still 0% to 40%. Punctuation now gives real pauses, but the clarity limit of this voice model remains. |
| 2.7 | A setting now chooses the voice provider: `TTS_PROVIDER=yarngpt` (the GPU host), `elevenlabs`, `azure` (Nigerian English voices) or `spitch` (English, Yoruba, Hausa and Igbo voices; chosen as the provider to try first), for the languages in `TTS_PROVIDER_LANGUAGES` (default English). Other languages keep the GPU voice, and the audio cache is keyed by voice so old audio is never replayed. | Covered by `tests/test_tts_providers.py`. | The same read-back check can score any provider, so the choice is made on numbers. |
| 2.8 | The same 16-phrase read-back check with Spitch (voice `jude`, English). Its audio is a streamed WAV with an unknown length and an extra chunk, so SOFA rewrites the header (`_repair_wav`). | `TTS_PROVIDER=spitch`, checked with `scripts/voice_check.py --hosted`. | 96% of words came back (31% with YarnGPT, 22% with its old default voice). Every one- and two-word reply ("Yes.", "Okay.", "Done.", "Thank you.", "Where should we deliver?") came back at 100%. The lowest were the long order read-back with numbers (78%) and "Okay. Where should we deliver the order to?" (75%). Phrases take 0.5 s to 8 s of audio, 24 kHz. |
| 2.9 | Long replies lost words: with a statement and a question in one clip, the closing "Anything else?" went unspoken, and a recording played back differently each time (the opening "Done." was heard on some plays and not others). The saved file was identical on every fetch, so the difference came from the player: a speaker that wakes up when playback starts swallows the first moments of a clip. | Hosted voices now make every sentence as its own clip and join them with 0.3 s of quiet (`split_sentences`, `join_wavs`), with 0.25 s of quiet before the first word. Spitch's limit of 3 calls at once is respected, with one retry after a 429. | The reply to the order read-back came back at 89% (78% before) and the closing message at 100% (95% before), with "Anything else?" and "Thank you, Bola." heard. Each clip starts its sound at 0.26 s. |

## 3. Speech recognition (N-ATLaS Whisper)

| # | Finding | Change | Effect |
|---|---|---|---|
| 3.1 | Accuracy on synthetic speech is poor and takes about 2 s a turn. Real phone recordings are needed to judge it. | The word-error-rate check (`scripts/asr_gate.py`) is ready and runs per language. | Pending real call recordings. |

## 4. Languages

| # | Finding | Change | Effect |
|---|---|---|---|
| 4.1 | Yoruba is understood end to end on the real model (a full Yoruba call placed an order: "Mo fe ra ajile NPK meji" was read as two bags of NPK, "Mejila Allen Avenue, Ikeja" as the address, the name was kept). SOFA's reply wording is not translated yet: the translation sheet has no Yoruba filled in, so a Yoruba caller hears English replies in the English voice. | Open: draft the Yoruba wording with N-ATLaS (the import already rejects any wording that loses a placeholder or number), then have it checked by a native speaker. | Pending. |

## 5. Hosting the models

| # | Finding | Change | Effect |
|---|---|---|---|
| 5.1 | A newer GPU family (Blackwell, RTX PRO 6000) fails to start the language model: its engine needs a newer CUDA toolkit than the host image has. | None: the 4090 and the L4 are used. | Both work with the same setup. |
| 5.2 | The first load of the language model from network storage takes about 10 minutes, longer than the start script waited. | The pod's start command raises the waits and starts all three models by itself. | All three services come up unattended. |
| 5.3 | A placeholder web server on the host held the language model's port. | The start command removes it first. | The model starts. |
| 5.4 | The L4 (24 GB) holds the language model, speech recognition and voice together, and costs a third less than the 4090. It is slower for the language model (2.0 to 2.6 s against 0.85 s). | A 4090 is preferred when one is available. | Both cards pass the same checks. |

## 6. Tools added for measurement

`scripts/gpu_smoke.py` (checks every service through SOFA's own clients), `gpu/bench_tts.py` (voice speed), `scripts/voice_check.py` (the recogniser reads back what the voice said), `scripts/simulate_call.py` (a whole call, now following the holding messages until the real answer arrives), `scripts/review_runs.py` (opens each saved run of the test call in its own dashboard, to listen and compare voices).

## Open improvements, in order of value

1. Shop understanding: move to the compact format (1.3). Plain yes, no and "that is all" are already handled without the model (1.7).
2. Short replies: solved by the Spitch voice (2.8); keep YarnGPT as the fallback voice for languages Spitch does not cover.
3. Yoruba reply wording (4.1).
4. Voice speed beyond real time: serve the voice model through vLLM, and start playing the first sentence while the rest is made.
5. Recorded fixed phrases (greeting, holding messages, goodbyes) in the chosen voice, so they play at once.
6. Speech recognition on real phone recordings (3.1).
7. A farm profile for each caller and advice drawn from a knowledge base with search (planned in the platform story).
