"""Curated sources to follow with one click (the Discover page).

Every YouTube channel id and feed URL here was resolved and fetched successfully on 2026-09-25
(YouTube handles were resolved to their UC… ids through the channel page, then the public Atom
feed was read). Subreddits are long-standing communities.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from radar.db.models import Source


def _r(sub: str, note: str) -> dict:
    return {"kind": "reddit", "identifier": sub, "name": f"r/{sub}", "note": note}


def _yt(channel_id: str, name: str, note: str) -> dict:
    return {"kind": "youtube", "identifier": channel_id, "name": name, "note": note,
            "url": f"https://www.youtube.com/channel/{channel_id}"}


def _rss(url: str, name: str, note: str) -> dict:
    return {"kind": "rss", "identifier": url, "name": name, "note": note}


CATALOG: list[dict] = [
    {"topic": "AI & LLMs", "items": [
        _r("LocalLLaMA", "Local models, hardware and benchmarks"),
        _r("MachineLearning", "Research papers and discussion"),
        _r("ClaudeAI", "Claude and coding agents"),
        _r("OpenAI", "OpenAI products and news"),
        _r("singularity", "AI progress, broad and fast"),
        _r("StableDiffusion", "Image and video generation"),
        _yt("UCNJ1Ymd5yFuUPtn21xtRbbw", "AI Explained", "Careful analysis of new models and papers"),
        _yt("UChpleBmo18P08aKCIgti38g", "Matt Wolfe", "Weekly AI news roundups"),
        _yt("UCbfYPyITQ-7l4upoX8nvctg", "Two Minute Papers", "Research papers in a few minutes"),
        _yt("UCXUPKJO5MZQN11PqgIvyuvQ", "Andrej Karpathy", "How LLMs work, from the ground up"),
        _yt("UCgfe2ooZD3VJPB6aJAnuQng", "bycloud", "AI research explained"),
        _yt("UCZHmQk67mSJgfCCTn7xBfew", "Yannic Kilcher", "Paper reviews and ML news"),
        _rss("https://simonwillison.net/atom/everything/", "Simon Willison", "Hands-on LLM notes, daily"),
        _rss("https://huggingface.co/blog/feed.xml", "Hugging Face blog", "Open models and tooling"),
        _rss("https://www.latent.space/feed", "Latent Space", "AI engineering newsletter"),
        _rss("https://openai.com/news/rss.xml", "OpenAI News", "Official announcements"),
        _rss("https://blog.google/technology/ai/rss/", "Google AI", "Official announcements"),
    ]},
    {"topic": "Programming", "items": [
        _r("programming", "Articles developers are reading"),
        _r("webdev", "Frontend, backend, tooling"),
        _r("rust", "Rust language and ecosystem"),
        _r("golang", "Go language and ecosystem"),
        _r("Python", "Python language and ecosystem"),
        _r("ExperiencedDevs", "Career and engineering practice"),
        _yt("UCsBjURrPoezykLs9EqgamOA", "Fireship", "Fast dev news (Code Report)"),
        _yt("UCUyeluBRhGPCW4rPe_UvBZQ", "The PrimeTime", "Reacting to dev news and articles"),
        _yt("UCbRP3c757lWg9M-U7TyEkXA", "Theo - t3.gg", "Web development news and opinions"),
        _yt("UC6biysICWOJ-C3P4Tyeggzg", "Low Level", "Systems programming and security"),
        _rss("https://lobste.rs/rss", "Lobsters", "Computing-focused link aggregator"),
        _rss("https://github.blog/feed/", "GitHub Blog", "GitHub product and engineering"),
    ]},
    {"topic": "Linux & open source", "items": [
        _r("linux", "Linux news and discussion"),
        _r("archlinux", "Arch Linux"),
        _r("opensource", "Open-source projects"),
        _r("selfhosted", "Running your own services"),
        _r("homelab", "Home servers and networks"),
        _yt("UC5UAwBUum7CPN5buc-_N1Fw", "The Linux Experiment", "Weekly Linux desktop news"),
        _yt("UCVls1GmFKf6WlTraIb_IaJg", "DistroTube", "Linux, window managers, free software"),
        _yt("UCR-DXc1voovS8nhAvccRZhg", "Jeff Geerling", "Raspberry Pi, homelab, hardware hacking"),
        _yt("UC4w1YQAJMWOz4qtxinq55LQ", "Level1Techs", "Servers, Linux and hardware deep dives"),
        _rss("https://www.phoronix.com/rss.php", "Phoronix", "Linux hardware and kernel news"),
        _rss("https://lwn.net/headlines/rss", "LWN.net", "Kernel and free-software journalism"),
        _rss("https://itsfoss.com/rss/", "It's FOSS", "Linux tips and news"),
        _rss("https://www.omglinux.com/feed/", "OMG! Linux", "Linux desktop news"),
    ]},
    {"topic": "Hardware", "items": [
        _r("hardware", "CPU, GPU and component news"),
        _r("buildapc", "PC building"),
        _r("nvidia", "NVIDIA"),
        _r("Amd", "AMD"),
        _yt("UChIs72whgZI9w6d6FhwGGHA", "Gamers Nexus", "Hardware testing and news"),
        _yt("UCI8iQa1hv7oV_Z8D35vVuSg", "Hardware Unboxed", "GPU and CPU benchmarks"),
        _yt("UCXuqSBlHAE6Xw-yeJA0Tunw", "Linus Tech Tips", "PC hardware and tech culture"),
        _rss("https://www.tomshardware.com/feeds/all", "Tom's Hardware", "Hardware news and reviews"),
        _rss("https://hackaday.com/blog/feed/", "Hackaday", "Hardware hacks and projects"),
    ]},
    {"topic": "Security", "items": [
        _r("netsec", "Technical security news"),
        _r("cybersecurity", "Security industry and careers"),
        _yt("UC9x0AN7BWHpCDHSm9NiJFJQ", "NetworkChuck", "Networking, Linux, security"),
        _rss("https://krebsonsecurity.com/feed/", "Krebs on Security", "Investigative security journalism"),
        _rss("https://www.bleepingcomputer.com/feed/", "BleepingComputer", "Breaches, malware, patches"),
        _rss("https://feeds.feedburner.com/TheHackersNews", "The Hacker News", "Security news"),
    ]},
    {"topic": "Tech news", "items": [
        _r("technology", "Big tech stories"),
        _r("gadgets", "Devices and launches"),
        _yt("UCeeFfhMcJa1kjtfZAGskOCA", "TechLinked", "Short daily tech news"),
        _yt("UCddiUEpeqJcYeBxX1IVBKvQ", "The Verge", "Tech news and reviews"),
        _yt("UCBJycsmduvYEL83R_U4JriQ", "Marques Brownlee", "Device reviews"),
        _rss("https://www.theverge.com/rss/index.xml", "The Verge", "Tech news"),
        _rss("https://feeds.arstechnica.com/arstechnica/index", "Ars Technica", "In-depth tech news"),
        _rss("https://www.wired.com/feed/rss", "Wired", "Tech and culture"),
        _rss("https://www.engadget.com/rss.xml", "Engadget", "Consumer tech"),
        _rss("https://www.technologyreview.com/feed/", "MIT Technology Review", "Emerging technology"),
        _rss("https://www.theregister.com/headlines.atom", "The Register", "Enterprise tech, with bite"),
    ]},
    {"topic": "Türkçe teknoloji", "items": [
        _rss("https://www.webtekno.com/rss.xml", "Webtekno", "Teknoloji haberleri"),
        _rss("https://shiftdelete.net/feed", "ShiftDelete.Net", "Teknoloji haberleri ve incelemeler"),
        _rss("https://www.donanimhaber.com/rss/tum/", "DonanımHaber", "Donanım ve teknoloji haberleri"),
    ]},
    {"topic": "Science & explainers", "items": [
        _yt("UCYO_jab_esuFRV4b17AJtAw", "3Blue1Brown", "Visual math and ML intuition"),
        _yt("UCHnyfMqiRRG1u-2MsSQLbXA", "Veritasium", "Science explainers"),
        _yt("UCKWaEZ-_VweaEx1j62do_vQ", "IBM Technology", "Plain-language tech explainers"),
    ]},
]


def source_name(entry: dict) -> str:
    prefix = {"reddit": "Reddit", "youtube": "YouTube", "rss": None}[entry["kind"]]
    label = entry["identifier"] if entry["kind"] == "reddit" else entry["name"]
    return f"{prefix} · {label}" if prefix else entry["name"]


def catalog(session: Session) -> list[dict]:
    """The catalog with each entry's follow state (matched case-insensitively on kind + identifier)."""
    rows = session.execute(select(Source.id, Source.kind, func.lower(Source.identifier), Source.status)).all()
    known = {(kind, ident): (sid, status) for sid, kind, ident, status in rows}
    out = []
    for group in CATALOG:
        items = []
        for entry in group["items"]:
            sid, status = known.get((entry["kind"], entry["identifier"].lower()), (None, None))
            items.append({**entry, "source_id": sid, "source_name": source_name(entry),
                          "following": sid is not None and status in ("active", "trial")})
        out.append({"topic": group["topic"], "items": items})
    return out
