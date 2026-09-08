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

- **Decided (2026-09-06):** hot-link the image from the outlet, with a fallback
  silhouette; copy nothing.
- Expect coverage of maybe a third of stories; TV station pages usually have one,
  wire and aggregator pages usually don't.

## 2. "In detail" module at the top of the stories page

A featured-story card above the table: mug shot, name, age, city/state, new offense,
prior record counts and named priors, status at offense, the summary, and source
links. Selection rule to decide:

- **Done (2026-09-08)** as the hero on the stories page. Auto-selects the newest
  strict, violent story with a photo. Hand-pick with `HERO_ID` at the top of the
  script in `site/index.html`; preview any record as the hero with `?hero=ID`.

## 3. Clean up the stories page; title it "Underincarceration Stories"

**Redesigned (2026-09-08):** hero story, then filters, then expandable cards, 20 at a
time, newest incident first. Sort options: newest, oldest, recently added, most prior
arrests, most prior convictions. `#id=N` pins one record with a "show all" link. The
summary tiles and monthly chart moved off this page (the chart is available on the
Facts page via `{chart:by_month}`).

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

| Option | Free tier (Sept 2026) | Effort | Notes |
|---|---|---|---|
| Kit (formerly ConvertKit) | 10,000 subscribers, unlimited sends | 10 min | embeddable HTML form; Kit branding on emails |
| beehiiv | 2,500 subscribers | 10 min | embeddable form |
| Substack embed | unlimited | 10 min | a publishing platform, not just a list |
| MailerLite | 250 subscribers | 20 min | too small |
| Buttondown | 100 subscribers | 10 min | too small |

- **Parked (2026-09-06)** until the initial list is built and content exists. Kit is the
  likely pick (10,000 free subscribers) unless the list should live with a Substack.

## 6. Style the whole site

One stylesheet shared by all pages (`site/site.css`), a name mark, a type pairing, a
restrained palette (the current blue is a placeholder), consistent header and footer
with navigation, responsive tables, and a dark mode that is chosen rather than
inverted. Do this last so it covers all pages at once.

- **Parked** pending brand constraints from the maintainer.

## Pipeline items carried from the plan

- About 30% of fetched articles yield no text (paywalls, JavaScript-only pages). A
  second fetch path (AMP URL, or a text-only mirror) would recover some.
- The daily schedule resumed 2026-09-08 (`daily.yml`, 10:30 UTC).
  Runs are capped at 150 articles (`max_classify`), about $1/day. Only strict stories
  are stored since 2026-09-08 (`STRICT_ONLY`); a strict-only headline triage would cut
  cost further but lose about two thirds of strict stories, so it was not done.
- Feedback triage from public issues is ported (2026-09-08): every card and the hero
  carry a flag link to the `flag-error.yml` issue form with the record id filled in;
  `feedback.yml` runs `review_feedback.py` (Sonnet) on new `feedback` issues and posts
  an assessment plus a `triage:*` label. Needs the `ANTHROPIC_API_KEY` secret, which
  the other workflows already use.
