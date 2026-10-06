"""Pre-generate cached audio for every fixed phrase at deploy time (latency budget).

    python -m scripts.warm_cache

Only languages with real wording are warmed (English always; Yoruba, Hausa and Igbo only for the
keys that have translations). Nothing English is ever spoken in a Yoruba/Hausa/Igbo voice.
"""

import asyncio

from sofa.config import get_settings
from sofa.container import build_services
from sofa.db import make_engine, make_session_factory
from sofa.dialogue import language, templates


async def main() -> None:
    settings = get_settings()
    svc = build_services(settings, make_session_factory(make_engine(settings.database_url)))
    count = 0
    for attempt in range(1, language.MAX_ASKS + 1):  # the spoken "which language?" question
        for text, vlang in language.ask_prompts(settings.languages, attempt):
            await svc.audio.speak(text, vlang)
            count += 1
    for lang in settings.languages:
        for key, variants in templates.EN.items():
            for i, english in enumerate(variants):
                if "{" in english or not templates.has(key, lang):
                    continue
                text = templates.pick(key, lang, i)
                if "{" in text:
                    continue
                await svc.audio.speak(text, lang)
                count += 1
    print(f"cached {count} fixed phrases")


if __name__ == "__main__":
    asyncio.run(main())
