# Medium Publication Assistant

A private, local, dry-run-first utility for finding suitable Medium publications for existing stories and managing a deliberately conservative review and submission queue.

It uses a persistent Chromium profile through Playwright. Sign-in happens directly in Medium's browser UI: the application does not ask for, copy, or store passwords, cookies, or session tokens. All state it owns is stored locally in SQLite.

## Safety model

- Dry-run mode is on by default.
- A story-publication pair must meet the configured score threshold (default 75).
- `approve <match-id>` approves only that exact pair.
- Applications require a separately displayed draft and explicit `--approve`; approval does not send it.
- Submission reopens and hashes the guidelines immediately before using Medium's normal UI.
- Inactive publications, unpublished-only conflicts, closed submissions, existing-publication matches, and pending duplicate submissions are hard rejections.
- Daily and weekly submission caps default to 3 and 10.
- CAPTCHA, security prompts, changed guidelines, missing/ambiguous controls, and external forms stop for manual intervention.
- Failed browser actions preserve screenshots and HTML under `artifacts/`.
- The utility never edits story text or tags and never removes a story from a publication.

## Setup

Python 3.11 or later is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
python app.py init
```

Keep `MEDIUM_DRY_RUN=true` for initial setup. The profile URL defaults to `https://medium.com/@nattupi`.

## First browser sign-in

Run:

```powershell
python app.py sync-stories
```

A persistent Chromium window opens using `playwright-profile/`. If Medium asks you to sign in, complete that manually in the browser. Rerun the command after sign-in if the first run did not reach the profile. Do not copy authentication values into `.env`.

Only one process should use this profile at a time. Close another Playwright window before starting a command.

## Commands

```powershell
python app.py sync-stories
python app.py discover-publications
python app.py match
python app.py review
python app.py approve <match-id>
python app.py reject <match-id>
python app.py apply <publication-id>
python app.py apply <publication-id> --approve
python app.py apply <publication-id> --send
python app.py submit <match-id>
python app.py status
python app.py report
```

`apply <publication-id>` prepares and prints the exact destination, message, and referenced stories. Inspect it before running the separate `--approve` command. `--send` is blocked in dry-run mode and stops for external forms, email, or any workflow that cannot be identified safely.

`submit` is blocked in dry-run mode even when a match is approved. To enable live external actions, deliberately set `MEDIUM_DRY_RUN=false` in `.env`. Approval records remain local and auditable.

### Discovery sources

The default starting point is Medium Help Center's monthly-updated list of publications in the Boost Nomination Program, including their submission-guideline links. The web changes frequently, so an alternate current official list or a specific curated source can be supplied:

```powershell
python app.py discover-publications --source "https://medium.com/path/to/current-list"
```

Every candidate is verified from its own page. A publication is `active` only when a visible dated post falls within `MEDIUM_ACTIVE_DAYS` (60 by default). When activity cannot be established it remains `uncertain`, not active. Requirements that are not explicit remain unknown rather than being guessed.

## Matching

Scores are 0–100:

| Signal | Weight |
|---|---:|
| Existing Medium tag overlap | 40 |
| Title/subtitle topic context | 25 |
| Previously published story acceptance | 20 |
| Recent activity | 10 |
| Preferred publication size | 5 |

Matching is deterministic and local. It does not rewrite stories. The title/subtitle vocabulary expansion is intentionally small and transparent in `medium_tool/matching.py`; Medium tags remain the primary signal. Unknown published-story acceptance receives partial credit but cannot silently override an explicit unpublished-only rule.

Each match stores component points, matched terms, and rejection reasons in SQLite.

## Data and audit trail

The default database is `data/medium_publisher.db`. Migration 1 creates:

- `stories`
- `publications`
- `guidelines`
- `matches`
- `writer_applications`
- `submissions`
- `approvals`
- `browser_actions`
- `errors`
- `schema_migrations`

URLs and partial unique indexes make sync, application preparation, approvals, and pending submissions idempotent. Browser actions record destination, dry-run state, result, and structured details.

Generate the initial report after the import/discovery/match sequence:

```powershell
python app.py sync-stories
python app.py discover-publications
python app.py match
python app.py report
```

The report is written to `artifacts/dry-run-report.md` and includes imported stories and tags, active candidates, up to three eligible matches per story, all rejected matches and reasons, application requirements, and stories with no reliable match.

## Tests

The tests use local mocked HTML fixtures; they do not access Medium or perform browser actions.

```powershell
python -m pytest
```

## Limitations

- Medium has no stable public DOM contract. Accessible labels are used where possible, but UI changes can require selector maintenance.
- Some profile pages use infinite scrolling and may not expose every older story in one session. Increase `MEDIUM_MAX_SCROLLS` and rerun; URL upserts avoid duplicates.
- Follower counts, publication identity, dates, and tags are recorded only when visible in page markup.
- Guideline prose cannot always be classified safely. Unknown values remain unknown and should be reviewed manually.
- The discovery crawler is intentionally bounded to links on supplied source pages; it is not a general web crawler.
- External forms and email are prepared but intentionally left for manual completion.
- Acceptance/rejection updates from editors are not inferred automatically; update tracking after reviewing Medium.
- No automation bypasses CAPTCHA, rate limits, access controls, or Medium submission limits.

## Recovery

- Authentication expired: rerun `sync-stories` headfully and sign in manually.
- Browser profile locked: close the Playwright Chromium window and rerun.
- CAPTCHA/security screen: complete it manually, inspect the saved artifacts, then rerun the stopped command.
- Guidelines changed: rerun `discover-publications`, then `match` and review/approve the new match.
- Ambiguous selector: do not repeatedly click. Inspect the failure screenshot/HTML in `artifacts/` and update the relevant accessible locator.
- Interrupted command: rerun it. Database upserts and unique indexes prevent duplicate imported records, open applications, and pending story submissions.
- Database backup: close running commands and copy `data/medium_publisher.db` plus any `-wal`/`-shm` files together.
- Full local reset: move `data/`, `artifacts/`, and `playwright-profile/` to a backup location, then run `python app.py init`. The browser profile contains local session state, so protect it like any signed-in browser profile.
