"""Write the translation sheet for a Yoruba / Hausa / Igbo speaker.

    python -m scripts.export_translation_sheet                 # docs/translation_sheet.xlsx
    python -m scripts.export_translation_sheet --out mine.xlsx

Existing translations (sofa/dialogue/translations.json) are pre-filled, so you can re-export after
changing an English sentence and send the translator only what is new.
"""

import argparse
from pathlib import Path

from sofa.config import get_settings
from sofa.dialogue import templates
from sofa.dialogue.translation_io import export_xlsx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/translation_sheet.xlsx")
    args = ap.parse_args()
    langs = tuple(l for l in get_settings().languages if l != "en")  # only what is switched on (ENABLED_LANGUAGES)
    rows = export_xlsx(Path(args.out), templates.TRANSLATIONS, langs)
    print(f"wrote {args.out}: {rows} rows for {len(templates.translatable_keys())} phrases, columns: {', '.join(langs)}")


if __name__ == "__main__":
    main()
