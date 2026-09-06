# To-do

Front-end and content work, in the order it should probably happen. Items marked
**needs you** wait on a decision or text from the maintainer.

## 1. Sweep stories for mug shots

Add a `mugshot_url` column and a pipeline stage that revisits each story's article
page, collects candidate images (`og:image`, images whose `src`, `alt`, or caption
mentions mugshot, booking, jail, sheriff, or the offender's name), and asks Claude
(Haiku, with vision) whether each is a booking photo of one person. Store the best
URL; blank when none. Runs on new stories in the daily job and once over the
existing database.

- **needs you:** hot-link the image from the outlet, or copy it into `site/mugshots/`?
  Hot-linking breaks when outlets move images and some block it; copying raises the
  rights question (booking photos are public records in most states, but the copy on
  the outlet's server is theirs). Recommendation: store the URL, hot-link with a
  fallback silhouette, and copy nothing.
- Expect coverage of maybe a third of stories; TV station pages usually have one,
  wire and aggregator pages usually don't.

## 2. "In detail" module at the top of the stories page

A featured-story card above the table: mug shot, name, age, city/state, new offense,
prior record counts and named priors, status at offense, the summary, and source
links. Selection rule to decide:

- **needs you:** newest strict story with a mug shot? Hand-picked (a `featured` flag
  or a list of ids in a small JSON file the page reads)? Rotate through the last N?
  Recommendation: a `data/featured.json` list of ids you edit, falling back to the
  newest strict story with a mug shot when the list is empty.

Depends on 1.

## 3. Clean up the stories page; title it "Underincarceration Stories"

Rename, tighten the table (fewer columns by default, an expandable row for the full
record), sortable columns, a per-story permalink (`stories.html#id=123`) so a single
record can be shared, and a proper share card (`og:image`, description). Move the
data-download links and caveats to a footer shared by all pages.

## 4. "Underincarceration Facts" page

A static page whose text you write. Build the shell now: headings, a sidebar table of
contents, a place for charts drawn from `stats.json` (release status breakdown, prior
count distribution, stories per month), and footnote styling for citations.

- **needs you:** the text. Markdown is easiest; a build step turns
  `content/facts.md` into `site/facts.html` so you never edit HTML.

## 5. Homepage with links and an email sign-up

`site/index.html` becomes the homepage: one-paragraph mission, three headline numbers
from `stats.json`, cards linking to Stories and Facts, the featured story, and the
sign-up form. The stories page moves to `site/stories.html`.

### 5a. Email sign-up backend

GitHub Pages is static, so the form posts somewhere else. Options, cheapest first:

| Option | Cost | Effort | Notes |
|---|---|---|---|
| Buttondown form | free to 100 subscribers, then $9/mo | 10 min | plain HTML form POST; sends the newsletter too |
| Substack embed | free | 10 min | if the commentary site already has one, reuse it |
| Mailchimp embedded form | free to 500 | 20 min | heavier script |
| Cloudflare Worker + KV + your own mailer | free tier | half a day | full control, more to maintain |

- **needs you:** which. Recommendation: Buttondown unless you already have a Substack
  list, in which case that.

## 6. Style the whole site

One stylesheet shared by all pages (`site/site.css`), a name mark, a type pairing, a
restrained palette (the current blue is a placeholder), consistent header and footer
with navigation, responsive tables, and a dark mode that is chosen rather than
inverted. Do this last so it covers all pages at once.

- **needs you:** any brand constraints (colors, fonts, a logo) or a site you want it
  to feel like.

## Pipeline items carried from the plan

- About 30% of fetched articles yield no text (paywalls, JavaScript-only pages). A
  second fetch path (AMP URL, or a text-only mirror) would recover some.
- The daily schedule is paused (`daily.yml`); resume when the backfill is done and
  the cost is acceptable (~$3/day at current density).
- Feedback triage from public issues (`review_feedback.py` in the Flock repo) is not
  ported yet.
