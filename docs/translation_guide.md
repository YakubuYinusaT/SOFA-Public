# Translating Sofa into Yoruba

Sofa is a voice agent for shops. Her English sentences are written; the Yoruba ones need a native speaker. (The pilot
runs English and Yoruba. Hausa and Igbo are switched off for now; when they are enabled, the same sheet gets a column for each.) This is how the work flows. The sheet is `docs/translation_sheet.xlsx` (also downloadable from the
**Languages** page of the admin console).

## For the translator (send them the sheet and this section)

Open the **Translate** sheet. Each row is one sentence Sofa says on the phone. Fill the yellow column (Yoruba).

1. Keep every `{placeholder}` exactly as written, curly brackets included. Move it where your grammar needs it. The
   "what {placeholders} will contain" column shows what gets filled in.
2. Never type numbers or prices yourself. Prices, quantities and product names are filled in automatically.
3. Short: one or two short sentences. People forget long sentences on a phone.
4. If the English is a question, yours ends with `?`. If it is not, it does not.
5. Sound like a friendly, polite shop attendant, not a machine translation. Never blame the caller.
6. Rows marked **Optional extra** are another way to say the same thing. Sofa rotates the wordings so she does not sound
   repetitive. Please fill them where you can (three wordings per common phrase is the target).
7. Leave a cell **empty** if you do not want to translate it. Sofa will use English for that sentence.
8. Use the **notes** column for anything unclear, or how customers really say something (for example the local word for a carton).

Do the rows marked **Required (most used)** first; they are at the top and cover nearly every call.

Things not translated in this version: product names, prices, numbers and unit words (carton, pack, tin, sachet). They are
read out in English inside your sentence. How prices should be spoken in each language is still an open decision: tell us
what your customers would expect.

## For you (the developer)

```bash
python -m scripts.export_translation_sheet                          # regenerate the sheet (existing translations pre-filled)
python -m scripts.import_translations docs/filled_sheet.xlsx --dry-run   # check only
python -m scripts.import_translations docs/filled_sheet.xlsx        # validate, then write sofa/dialogue/translations.json
```

Or use **Admin > Languages**: download the sheet, upload the filled one, choose "just check it" first.

The import checks every cell. It reports the language, phrase, variant and spreadsheet row of each problem, and by default
writes nothing if any cell is wrong (`--allow-partial` keeps only the valid cells):

- the `{placeholders}` must match the English exactly (an extra wording may use any placeholder the English uses);
- no digits unless the English has them;
- a question stays a question;
- no stray curly brackets.
It also warns about very long sentences, cells identical to the English, and Yoruba phrases with fewer than three wordings.

### What happens for a phrase that is not translated

Sofa says it in English **with the English voice**, never English text in a Yoruba/Hausa/Igbo voice. A caller who is
detected as speaking Yoruba therefore hears a mix until the sheet is complete. Check progress on the dashboard or the Languages page.
Reply mode: Yoruba stays on templates (the model card rates Yoruba generation weakest); English, Hausa and Igbo let
N-ATLaS word only the clarification questions, and only if the validator accepts them (see `sofa/dialogue/validator.py`).

### Ask a native speaker to listen too

Translations are only half of it. Have the same person play the TTS output from `python -m scripts.gpu_smoke` for each language
and say whether YarnGPT2's voice is acceptable. If it is not, the spec's fallback is pre-recorded human phrases.

### After importing

Restart the server (the import updates the running process only), then `python -m scripts.warm_cache` to pre-generate the
audio for the new fixed phrases.
