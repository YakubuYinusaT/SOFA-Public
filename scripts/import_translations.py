"""Import a filled-in translation sheet into sofa/dialogue/translations.json.

    python -m scripts.import_translations docs/translation_sheet.xlsx             # validate, then write
    python -m scripts.import_translations sheet.xlsx --dry-run                    # validate only
    python -m scripts.import_translations sheet.xlsx --allow-partial              # keep valid cells, skip bad ones

Without --allow-partial nothing is written if any cell has an error, so a translator's mistake can
never reach a live call. Send the printed list of problems back to the translator.
"""

import argparse
import sys
from pathlib import Path

from sofa.config import get_settings
from sofa.dialogue.translation_io import LANG_NAMES, import_sheet


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sheet")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-partial", action="store_true")
    args = ap.parse_args()
    res = import_sheet(Path(args.sheet), allow_partial=args.allow_partial, dry_run=args.dry_run)

    for line in res.errors:
        print("ERROR  ", line)
    for line in res.warnings:
        print("warning", line)
    print()
    enabled = get_settings().languages
    for lang, (done, total) in res.coverage().items():
        if lang not in enabled:
            continue
        print(f"{LANG_NAMES[lang]:8} {done}/{total} phrases translated" + ("" if done == total else "  (the rest fall back to English)"))
    if res.errors and not args.allow_partial:
        print(f"\n{len(res.errors)} error(s): nothing was written. Fix the cells above and re-run.")
        return 1
    print("\n" + ("dry run: nothing written" if args.dry_run else "written to sofa/dialogue/translations.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
