"""The overview pages (/overview): what Talk is, how it works, what it can do, and what testing the models showed.

Public and read-only. It holds no customer data and no passwords. The things that differ from one deployment to the next (who to email, the number to
call, the videos, the pitch deck, a note about what live testing costs) come from settings, and a block with nothing set is simply left out.
"""

import html
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from ..web import ui
from .pages import templates

router = APIRouter(prefix="/overview")

ROOT = Path(__file__).resolve().parent.parent.parent

# sector, what Talk provides, status, a thing a caller might say
SECTORS = [
    ("Banking and payments", "A bank wallet, or the bank you already use: send money, pay bills, buy airtime, check a balance.", "Live", "Send 5k to my brother."),
    ("Commerce", "Shops and dealers: prices, orders and payment in one call.", "Live", "I want two bags of NPK."),
    ("Information", "Answers to everyday questions, in your own language.", "Live", "How much is maize this week?"),
    ("Agriculture", "Farm inputs and trusted advice written by dealers and experts, from fertiliser use to planting time.", "Live", "When should I put fertiliser on my maize?"),
    ("Logistics", "Track parcels and book delivery and transport.", "Soon", "Where is my parcel?"),
    ("Health", "Medical help, or the nearest clinic and pharmacy.", "Soon", "Nearest clinic, abeg."),
    ("Utilities", "Light token, recharge, data and bills.", "Soon", "Help me buy light token."),
    ("Government", "Guidance on a driver's licence, a national ID or a permit, and what you need for each.", "Soon", "How do I renew my driver's licence?"),
    ("Emergency services", "Help when it matters most.", "Later", "Abeg, I need help now."),
]


def enabled(request: Request) -> None:
    """These pages describe shopping and search, so a build that has neither does not serve them."""
    s = request.app.state.svc.settings
    if not s.overview_enabled or ui.profile(s) == "demobank":
        raise HTTPException(404)


def video_kind(url: str) -> str:
    """'file' when the address is a video file the page can play itself, 'link' for anything else (YouTube, Drive...), '' when there is none."""
    if not url:
        return ""
    return "file" if re.search(r"\.(mp4|webm|mov)(\?.*)?$", url, re.I) else "link"


def inline(text: str) -> str:
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)


def markdown_to_html(md: str) -> str:
    """Just enough Markdown for the model log: headings, paragraphs, numbered lists and tables, with **bold** and `code`."""
    out, lines, i = [], md.replace("\r\n", "\n").split("\n"), 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("### "):
            out.append(f"<h3>{inline(line[4:])}</h3>")
            i += 1
        elif line.startswith("## "):
            out.append(f"<h2>{inline(line[3:])}</h2>")
            i += 1
        elif line.startswith("# "):
            i += 1  # the page has its own title
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            head, body = rows[0], [r for r in rows[2:]]
            th = "".join(f'<th scope="col">{inline(c)}</th>' for c in head)
            trs = "".join("<tr>" + "".join(f'<td data-label="{html.escape(head[k] if k < len(head) else "", quote=True)}">{inline(c)}</td>' for k, c in enumerate(r)) + "</tr>" for r in body)
            out.append(f'<div class="tablewrap"><table class="responsive"><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table></div>')
        elif re.match(r"\d+\. ", line):
            items = []
            while i < len(lines) and re.match(r"\d+\. ", lines[i]):
                items.append(re.sub(r"^\d+\. ", "", lines[i]))
                i += 1
            out.append("<ol>" + "".join(f"<li>{inline(t)}</li>" for t in items) + "</ol>")
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|")) and not re.match(r"\d+\. ", lines[i]):
                para.append(lines[i])
                i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
    return "\n".join(out)


def context(request: Request, **extra) -> dict:
    s = request.app.state.svc.settings
    return {"request": request, "brand": "Talk", "footer_kind": "naic", "home_url": "/overview", "active": "", "s": s,
            "story_kind": video_kind(s.hub_story_video_url), "demo_kind": video_kind(s.hub_demo_video_url),
            "demo_page": s.demo_page_enabled, **extra}


@router.get("")
def overview(request: Request, _=Depends(enabled)):
    return templates.TemplateResponse(request, "overview.html", context(request))


@router.get("/technology")
def technology(request: Request, _=Depends(enabled)):
    return templates.TemplateResponse(request, "overview_technology.html", context(request))


@router.get("/use-cases")
def use_cases(request: Request, _=Depends(enabled)):
    return templates.TemplateResponse(request, "overview_use_cases.html", context(request, sectors=SECTORS))


@router.get("/findings")
def findings(request: Request, _=Depends(enabled)):
    path = ROOT / "docs" / "model_log.md"
    body = markdown_to_html(path.read_text(encoding="utf-8")) if path.exists() else ""
    return templates.TemplateResponse(request, "overview_findings.html", context(request, body=body))
