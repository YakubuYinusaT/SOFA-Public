# Code-mixing: Yoruba, English and Pidgin in one sentence

In Lagos most callers do not stay in one language. "Mo fẹ́ carton méjì ti Indomie Super Pack" is Yoruba, English and a
Yoruba number in one breath. SOFA is built to expect that, not to treat it as an error.

## What the system does about it

1. **Yoruba words survive.** Speech recognisers write Yoruba with tone marks and dots under letters (mẹ́ta, jọ̀wọ́, ṣé).
   Our text cleaner used to *delete* those letters ("mẹ́ta" became "m ta"), so no Yoruba word was ever understood. It now
   reduces each letter to its plain base first (mẹ́ta -> meta). This was a real bug and is fixed and tested.
2. **Yoruba numbers and filler are understood** alongside English ones, so "carton méjì" reads as 2 cartons and "mo fẹ́ ... ti ..."
   is ignored filler around the product name.
3. **Both speech models hear every turn** (English and Yoruba, `DUAL_ASR=true`). A mixed sentence is often wrong on one
   model and right on the other. The more confident reading is the transcript and the other is passed to N-ATLaS as a
   second opinion ("ALSO HEARD ..."). Both readings are stored on the turn and shown in the admin console.
4. **A mixed first sentence is not interrupted** by the "which language?" question. Low speech confidence is normal for mixed
   speech, so Sofa only asks if she cannot find a request in what was said.
5. **Low confidence alone no longer rejects a turn.** It only does when the LLM cannot find a request either. The read-back
   before every order is what protects against a misheard quantity or product.
6. **The reply language follows the caller, calmly.** It changes only when the other model is clearly better (by
   `LANGUAGE_SWITCH_MARGIN`, default 0.3) on two turns in a row, or when the caller asks for a language by name in a short
   sentence ("English please", "Yoruba"). One English word never flips a Yoruba call.
7. **The LLM prompt says callers mix languages** and gives four examples.
8. **You can measure it.** Tick "code-switching" when labelling a turn. Metrics shows how many labelled turns per language were mixed.

## Please review (you speak Yoruba, I do not)

These lists are my best attempt and **need a Yoruba speaker's correction**. Anything wrong here means a real Yoruba word is
either not understood or mistaken for something else. The code is in `sofa/textutil.py` (`YORUBA_NUMBERS`, `FILLERS`),
`mocks/rules.py` and `CODE_MIXING_NOTE` in `sofa/dialogue/manager.py`.

**Numbers, in the plain-letter form the system sees** (tone marks and dots removed):

| Number | Forms accepted | Number | Forms accepted |
|---|---|---|---|
| 1 | kan, okan | 7 | meje |
| 2 | meji, eji | 8 | mejo |
| 3 | meta, eta | 9 | mesan |
| 4 | merin, erin | 10 | mewa, mewaa |
| 5 | marun | 20 / 30 / 40 / 50 / 100 | ogun / ogbon / ogoji / aadota / ogorun |
| 6 | mefa, efa | | |

Left out on purpose because they are also everyday words: ewa (beans), arun (illness), eje, ejo, esan. Is that the right
call? Are there common forms of the numbers people say when counting goods that are missing?

**Filler ignored when matching a product name:** jowo, abeg, mo, fe, fun, mi, ti, ni, ra, wan, dey, una, na, be.
Anything on that list that is also a real product word, or anything missing (for example "e" or "wa")?

**Yes / no in the test parser:** "bẹ́ẹ̀ ni" (yes), "rárá" (no). What else do people actually say to confirm or refuse an order
(Yoruba, Pidgin, and mixed)? Please add three or four of each.

**The four prompt examples** (in `CODE_MIXING_NOTE`) are illustrative. Better ones from real calls will improve N-ATLaS's
reading. Replace them once you have recordings.

## What is not solved, honestly

- **N-ATLaS's own model card says code-switching support is limited.** The measures above help, but they do not change what
  the models can do. The accuracy test on real, mixed phone audio is what tells us how well it works. Include mixed sentences
  in the 20 recordings.
- **The two models' confidence scores are not calibrated against each other.** "More confident" is a heuristic. The thresholds
  (`PRIMARY_MARGIN`, `LANGUAGE_SWITCH_MARGIN`) are guesses to be tuned on real calls; the stored readings make that possible.
- **Speech output.** Sofa's Yoruba sentences contain English product names, prices and units. A Yoruba TTS voice may pronounce
  those oddly. This is natural for how Lagosians speak, but check it by ear (`scripts/gpu_smoke.py`).
- **Product names and aliases.** Customers ask for the same item in Yoruba, English, Pidgin and mixed ways.
  When you collect aliases from a merchant in person, ask for all four (for example the local way to say "big Indomie") and
  add them in the catalog. Confirmed calls also teach new aliases automatically.
- **Pidgin** has no dedicated speech model. It is close enough to English that the English model handles much of it, but
  expect errors. Pidgin cues in the parser are minimal.
- **Only English and Yoruba are enabled.** Mixing Hausa or Igbo with English is not covered yet.
