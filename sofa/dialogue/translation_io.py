"""Translation sheet: export every spoken phrase for a translator, import the filled sheet back.

The sheet is a normal .xlsx (Excel or Google Sheets); a .csv with the same headers also imports.
Import validates every cell against the English so a translator cannot break a call: the same
{placeholders}, no invented numbers, a question stays a question. Nothing is written if there are
errors, unless --allow-partial is used, in which case only the valid cells are kept.
"""

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import templates
from .validator import PLACEHOLDER

LANG_NAMES = {"yo": "Yoruba", "ha": "Hausa", "ig": "Igbo"}
BASE_HEADERS = ["key", "variant", "status", "when it is spoken", "English", "what {placeholders} will contain"]
HEADERS = BASE_HEADERS + ["Yoruba", "Hausa", "Igbo", "notes"]  # the full sheet; export_xlsx can leave languages out
LANG_COLUMN = {"yo": "Yoruba", "ha": "Hausa", "ig": "Igbo"}
SINGLE_WORD = ("daypart_", "word_", "status_")
EXTRA_TARGET = 3  # phrasings per common phrase, so Sofa does not repeat herself (spec: 3 or 4 for Yoruba)
MAX_WORDS_WARN = 30

INSTRUCTIONS = [
    "SOFA translation sheet",
    "",
    "Sofa answers customers on the phone. These are the sentences she says. Please write each one in Yoruba, Hausa and Igbo, "
    "as a friendly, polite shop attendant in Nigeria would say it on a phone call.",
    "",
    "RULES (the import checks these and will list anything to fix):",
    "1. Keep every {placeholder} exactly as written, including the curly brackets. Move it wherever your grammar needs it. "
    "Do not translate the words inside the brackets. The sheet shows what each one will contain.",
    "2. Do not write any numbers or prices yourself. Numbers, prices, product names and quantities are filled in automatically.",
    "3. Keep it short: one or two short sentences. People forget long sentences on a phone.",
    "4. A question stays a question (end it with ?). A statement stays a statement.",
    "5. Sound natural, like a person, not like a machine translation. Never blame the caller (for example 'you did not speak clearly').",
    "6. Rows marked 'Optional extra' are another way to say the same thing. Please fill them where you can: Sofa rotates "
    "between the wordings so she does not sound repetitive. Leave a cell empty if you have nothing to add.",
    "7. Leave a cell EMPTY if you do not want to translate it: Sofa will then use English for that sentence.",
    "8. Fill the 'notes' column with anything unclear or any suggestion.",
    "",
    "WHAT IS NOT TRANSLATED HERE: product names, prices, numbers, and unit words like carton, pack, tin, sachet. "
    "These are read in English inside your sentence for now. If the shop's customers say them differently, write that in 'notes'.",
    "",
    "Do the rows marked Required first; the sheet is sorted with the most-used sentences at the top.",
]


@dataclass
class ImportResult:
    translations: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def coverage(self) -> dict[str, tuple[int, int]]:
        keys = templates.translatable_keys()
        return {l: (sum(1 for k in keys if self.translations.get(l, {}).get(k)), len(keys)) for l in LANG_NAMES}


def _ordered_keys() -> list[str]:
    keys = templates.translatable_keys()
    return sorted(keys, key=lambda k: (0 if k in templates.P1 else 1, keys.index(k)))


def _row_count(key: str) -> int:
    english = len(templates.EN[key])
    if key in templates.P1 and key != "ack" and not key.startswith(SINGLE_WORD) and key != "consent":
        return max(english, EXTRA_TARGET)
    return english


def _placeholder_help(text: str) -> str:
    names = sorted({m.group(1) for m in PLACEHOLDER.finditer(text)})
    return "\n".join(f"{{{n}}} = {templates.PLACEHOLDER_HELP.get(n, '(filled in automatically)')}" for n in names)


def sheet_rows(existing: dict[str, dict[str, list[str]]] | None = None, languages: tuple[str, ...] = ("yo", "ha", "ig")) -> list[list[str]]:
    existing = existing or {}
    rows = []
    for key in _ordered_keys():
        english = templates.EN[key]
        for v in range(_row_count(key)):
            extra = v >= len(english)
            source = english[v] if not extra else english[0]
            status = ("Optional extra" if extra else "Required") + (" (most used)" if key in templates.P1 else "")
            when = templates.CONTEXT.get(key, "")
            if extra:
                when = "ANOTHER WAY to say the same thing. " + when
            row = [key, v, status, when, source, _placeholder_help(" ".join(english))]
            for lang in languages:
                have = existing.get(lang, {}).get(key, [])
                row.append(have[v] if v < len(have) else "")
            row.append("")
            rows.append(row)
    return rows


def export_xlsx(path: Path, existing: dict[str, dict[str, list[str]]] | None = None,
                languages: tuple[str, ...] = ("yo", "ha", "ig")) -> int:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws0 = wb.active
    ws0.title = "Instructions"
    for i, line in enumerate(INSTRUCTIONS, 1):
        c = ws0.cell(row=i, column=1, value=line)
        c.alignment = Alignment(wrap_text=True, vertical="top")
        if i == 1:
            c.font = Font(bold=True, size=14)
    ws0.column_dimensions["A"].width = 110

    ws = wb.create_sheet("Translate")
    ws.append(BASE_HEADERS + [LANG_NAMES[l] for l in languages] + ["notes"])
    for row in sheet_rows(existing, languages):
        ws.append(row)
    widths = [22, 8, 20, 48, 48, 48] + [48] * len(languages) + [30]
    grey, yellow, head = (PatternFill("solid", fgColor=c) for c in ("EEEEEE", "FFF8DC", "D9E2F3"))
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for c in ws[1]:
        c.font, c.fill = Font(bold=True), head
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for r in ws.iter_rows(min_row=2):
        for c in r:
            c.alignment = Alignment(wrap_text=True, vertical="top")
            c.fill = grey if c.column <= 6 else yellow if c.column <= 6 + len(languages) else PatternFill()
    ws.freeze_panes = "G2"
    ws.auto_filter.ref = ws.dimensions

    wg = wb.create_sheet("Placeholders")
    wg.append(["placeholder", "what it will contain"])
    for name, text in templates.PLACEHOLDER_HELP.items():
        wg.append([f"{{{name}}}", text])
    wg.column_dimensions["A"].width = 18
    wg.column_dimensions["B"].width = 100
    for c in wg[1]:
        c.font = Font(bold=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return ws.max_row - 1


def read_sheet(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            return [dict(r) for r in csv.DictReader(f)]
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    ws = wb["Translate"] if "Translate" in wb.sheetnames else wb.active
    rows = list(ws.iter_rows(values_only=True))
    header = [str(h).strip() if h is not None else "" for h in rows[0]]
    return [dict(zip(header, r)) for r in rows[1:] if any(v not in (None, "") for v in r)]


def _clean(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def build(rows: list[dict]) -> ImportResult:
    res = ImportResult(translations={l: {} for l in LANG_NAMES})
    per_cell: dict[tuple[str, str], dict[int, str]] = {}
    for n, row in enumerate(rows, 2):
        key = _clean(row.get("key"))
        if not key:
            continue
        if key not in templates.EN or key in templates.NOT_TRANSLATED:
            res.errors.append(f"row {n}: unknown key '{key}' (do not edit the key column)")
            continue
        try:
            variant = int(row.get("variant"))
        except (TypeError, ValueError):
            res.errors.append(f"row {n} ({key}): variant must be a number (do not edit that column)")
            continue
        for lang, col in LANG_COLUMN.items():
            text = _clean(row.get(col))
            if not text:
                continue
            problems = check_cell(key, variant, text)
            label = f"{LANG_NAMES[lang]} / {key} / variant {variant} (row {n})"
            errs = [p for p in problems if not p.startswith("warning: ")]
            res.errors += [f"{label}: {p}" for p in errs]
            res.warnings += [f"{label}: {p[9:]}" for p in problems if p.startswith("warning: ")]
            if not errs:
                per_cell.setdefault((lang, key), {})[variant] = text
    for (lang, key), cells in per_cell.items():
        ordered = [cells[v] for v in sorted(cells)]
        if 0 not in cells:
            res.warnings.append(f"{LANG_NAMES[lang]} / {key}: only extra wordings filled, the Required row is empty")
        res.translations[lang][key] = ordered
    for lang in LANG_NAMES:
        for key in templates.P1:
            got = len(res.translations[lang].get(key, []))
            if key in res.translations[lang] and _row_count(key) >= EXTRA_TARGET and got < 2 and lang == "yo":
                res.warnings.append(f"Yoruba / {key}: {got} wording; Sofa sounds repetitive with fewer than 3 (spec)")
    return res


def check_cell(key: str, variant: int, text: str) -> list[str]:
    """Problems with one translated cell. Entries starting 'warning: ' do not block the import."""
    english = templates.EN[key]
    out: list[str] = []
    wanted_sets = [templates.placeholders(e) for e in english]
    union = set().union(*wanted_sets)
    got = templates.placeholders(text)
    leftover = PLACEHOLDER.sub("", text)
    if "{" in leftover or "}" in leftover:
        out.append("has a stray curly bracket: every { must belong to a full {placeholder}")
    if variant < len(english):
        required = wanted_sets[variant]
        if got != required:
            missing, extra = sorted(required - got), sorted(got - required)
            out.append("placeholders must match the English exactly"
                       + (f"; missing {' '.join(missing)}" if missing else "")
                       + (f"; not allowed {' '.join(extra)}" if extra else ""))
    elif not got <= union:
        out.append(f"placeholders not allowed here: {' '.join(sorted(got - union))}")
    if re.search(r"\d", leftover) and not any(re.search(r"\d", e) for e in english):
        out.append("contains a number: numbers are filled in automatically, please remove it")
    wants_question = any("?" in e for e in english)
    if wants_question != ("?" in text):
        out.append("the English is a question, so this must end with ?" if wants_question else "the English is not a question, remove the ?")
    words = len(text.split())
    if words > MAX_WORDS_WARN:
        out.append(f"warning: {words} words is long for a phone call")
    if text.strip().lower() == english[min(variant, len(english) - 1)].strip().lower() and words > 2:
        out.append("warning: identical to the English")
    return out


def write_json(translations: dict[str, dict[str, list[str]]], path: Path | None = None) -> Path:
    path = path or templates.TRANSLATIONS_PATH
    clean = {l: t for l, t in translations.items() if t}
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def import_sheet(path: Path, *, allow_partial: bool = False, dry_run: bool = False) -> ImportResult:
    res = build(read_sheet(path))
    if not dry_run and (allow_partial or not res.errors):
        write_json(res.translations)
        templates.load_translations()
    return res
