"""Banks SOFA can connect a caller to, and how callers say their names.

Banks are listed by code; a bank is connected when its adapter is registered (step 3).
"""

from dataclasses import dataclass

from ..textutil import clean


@dataclass(frozen=True)
class Bank:
    code: str
    name: str
    aliases: tuple[str, ...]  # how people say it, already in `clean()` form


BANKS: dict[str, Bank] = {
    b.code: b for b in (
        Bank("demobank", "Demo Bank", ("demo bank", "demobank")),
        Bank("gtbank", "GT Bank", ("gtbank", "gt bank", "gtb", "g t bank", "guaranty trust")),
        Bank("access", "Access Bank", ("access bank",)),  # not just "access": callers say that word for other reasons
    )
}


def mentioned(*texts: str, among: list[str] | None = None) -> list[Bank]:
    """Banks named in any of the texts: the main reading of the audio and the other models' readings of it."""
    pool = [BANKS[c] for c in among if c in BANKS] if among is not None else list(BANKS.values())
    found = []
    for bank in pool:
        for text in texts:
            padded = f" {clean(text)} "
            if any(f" {a} " in padded for a in bank.aliases):
                found.append(bank)
                break
    return found
