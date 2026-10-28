"""The Help area: one Markdown article per feature, from `app/help/`."""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends

from .. import modules

router = APIRouter(dependencies=[Depends(modules.require("help"))])

HELP_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "help")


@router.get("/api/help")
def get_help():
    """Every help article, in order. One Markdown file per feature under
    `app/help/`, so the articles ship and change with the code they describe;
    the filename orders them and its first `# ` line is the title. All of
    them at once, because together they are smaller than one calendar read
    and the page can then move between them without asking again."""
    articles = []
    for name in sorted(os.listdir(HELP_DIR)):
        if not name.endswith(".md"):
            continue
        with open(os.path.join(HELP_DIR, name), encoding="utf-8") as f:
            text = f.read()
        head, _, body = text.partition("\n")
        articles.append({"slug": name[:-3], "title": head.removeprefix("# ").strip(),
                         "body": body.strip()})
    return articles
