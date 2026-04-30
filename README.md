# Linh News

Personalized daily news edition for Linh — an HTML page and a one-page "Linh Times" PDF in the style of the New York Times.

Generated twice daily (`0 0` and `0 12` America/New_York) plus on-demand via a Refresh button. See [CLAUDE.md](CLAUDE.md) for architecture and `~/.claude/plans/let-s-create-a-news-pr-expressive-pinwheel.md` for the full plan.

## Quick start

```bash
uv sync
uv run uvicorn app.main:app --reload
```

## Authorized users

Edit [users.txt](users.txt) to grant Google-account access. Admin actions are restricted to `vtlinh87@gmail.com`.
