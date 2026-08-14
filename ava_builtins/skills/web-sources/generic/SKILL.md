---
name: generic
description: Extract any web page's main text (boilerplate stripped, to markdown plus metadata), or enumerate article links from an index page by URL regex. The fallback for sources with no platform adapter and no RSS — official sites, forums, newsletters.
---

# generic

The **universal fallback adapter** of the `web-sources` family (shared S1 item schema: [`../SKILL.md`](../SKILL.md)).
Platform accounts have dedicated adapters (`youtube`, plus the top-level `gmail` skill), RSS/Atom feeds have `rss`;
sources that **have neither platform API nor RSS feed** (official news sites, forum portals, blog index pages) fall here.

**A self-contained CLI script** (`reference/feed.py`): agent starts bash, passes command-line arguments, reads stdout
JSON. Fetching and list-page enumeration are **two-level cascades** (`fetch` and `enum`
automatically step through, no need for you to specify):

1. **curl + real browser UA + `trafilatura`** — many Chinese official/news sites will 403 bare clients or cause
   WebFetch to time out, but recognize normal browser UA; `trafilatura` does main content extraction + strips navigation/ads/footer →
   markdown + metadata. The vast majority of server-rendered sites are sufficient at this level.
2. **When hitting anti-bot / JS wall, upgrade to Jina Reader (`r.jina.ai`)** — server-side headless browser
   rendering to bypass the wall (Cloudflare challenge, "Enable JavaScript" shell). `fetched_via` will be marked as
   `web-scrape+jina`. If `JINA_API_KEY` (env) is set, it uses the keyed version (proxy rotation + forced rendering +
   higher rate limit); if not, uses the anonymous tier (usually sufficient for the open web; even OpenAI's anonymous can pass).

Only if both levels fail (login wall) then raise, prompting to use a logged-in browser (chrome MCP) or the corresponding platform adapter.
Pure index/login pages (no wall characteristics and no main content) directly report "use enum", **do not waste Jina**. `curl` +
`trafilatura` are the only two third-party dependencies; Jina is just an HTTP call, the rest is pure stdlib.

## Usage (agent invokes via bash)

```bash
# Fetch single page -> main text markdown + title/author/date/site, store in mirror, project S1:
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-sources/generic/reference/feed.py fetch --url <article-url>

# Enumerate article links from list/index page (--link-pattern is a regex for "which a tags are articles", matching absolute URL):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-sources/generic/reference/feed.py enum --url <list-page-url> --link-pattern 'mod=view&aid=\d+'

# One-stop: enumerate + fetch one by one + project S1 (mirror deduplication, see below):
$AVA_HOME/source/.venv/bin/python $AVA_HOME/skills/web-sources/generic/reference/feed.py sync --url <list-page-url> --link-pattern '...' --limit 20
```

Specifically which sites to scrape and what `--link-pattern` to use are **user preferences** and not written into this skill — the agent reads them from its own
memory (list of followed sources) and invokes one by one.

## `--link-pattern` is the key knob of this skill

A generic crawler **cannot guess** which links on a site are articles (navigation, ads, related links are mixed together). Therefore, during enumeration, the agent
provides a regex, matching the **absolutized href**, and only matches are considered article links. Example:

- Discuz forum portal: article URLs look like `portal.php?mod=view&aid=12345` →
  `--link-pattern 'mod=view&aid=\d+'`.
- Some newspaper digital edition: section/article URLs contain `content_...htm` → provide a regex according to its pattern.

If `--link-pattern` is not given, enumeration returns **all** links on the page (very noisy, basically only used when exploring a new site's structure).
Links are kept in order of appearance on the page (list pages are usually newest-first, so the first N are the latest N).

## Cursor model (mirror deduplication, no state file)

There is no persistent cursor file: each `sync` re-fetches the list page and fetches one by one, **deduplicating by whether the mirror directory already exists**
(URL → stable slug directory; if a previously fetched `post.json` exists, skip and do not re-fetch). Re-running the same list page is idempotent
— the same idea as gmail's `--since` window + mirror deduplication. In `sync`, if a certain link cannot extract main content
(actually a list page/login wall), it will **skip and log a line to stderr**, not interrupting the whole sync.

## Output schema

```
$AVA_HOME/state/mirrors/web-scrape/<slug>/   # slug = link tail segment + URL short hash (stable)
├── post.json    # platform/url/source_id/title/text/description/author{id=domain,name}/sitename/published_at/fetched_*
└── post.md      # rendered version (main text markdown)
```

S1 projection: `platform="web"`, `source_id` = page URL, `author.id` = site domain,
`matrix_entity` defaults to null (cross-platform same-source deduplication can be enabled by the caller with `--matrix-entity` as needed).

## What NOT to do / Boundaries

- **`enum` escalates too**: `enum` runs the same two-rung ladder as `fetch` — curl + browser UA
  first, then a Jina render when the list page 403s/times out or returns a wall shell. The Jina rung
  returns markdown, so links are harvested from its `[text](url)` links (logged as `web-scrape+jina`).
  If both rungs fail (a login wall, or a host that refuses even a rendered client — e.g. a
  geo/path-blocked page), switch to a platform skill, a locally-installed dedicated adapter, or
  chrome MCP.
- **Cannot bypass login wall / heavy interaction SPA**: Jina can bypass Cloudflare/JS rendering, but **cannot bypass login state**; content that requires login
  goes to the corresponding platform adapter or chrome MCP, not here.
- **No pagination / no recursive crawling**: `enum` only captures **the single list page you provide**. For multiple pages, invoke multiple times (give different page URLs).
- **SSRF fallback**: Reject `file://` / literal loopback/private IP; domain resolution to private network is not deeply followed (the driving agent is trusted,
  this is a tripwire not a sandbox).
- **Fail loudly if no main text can be extracted**: `fetch` on a list page/login wall will raise ("no main text to extract" is a true signal, not silently returning empty).
