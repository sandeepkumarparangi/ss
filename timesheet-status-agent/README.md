# Timesheet status agent

Checks the latest timesheet hourly and emails its from date, to date, and exact status when that status changes. Uses Python 3.11 or later and the standard library; no pip packages are required.

**Current state: code only. Live monitoring is disabled.** This project does not have a working Compass integration or mail connection. It does not submit, edit, or approve timesheets. The separate paused automation in the chat is not enabled by running this code.

## Location in the existing repository

This project is uploaded under `timesheet-status-agent/` in [sandeepkumarparangi/ss](https://github.com/sandeepkumarparangi/ss/tree/master/timesheet-status-agent). Open a terminal in that folder before running the commands below. The original Java project remains in its existing `ss/` folder.

The repository-level `.github/workflows/timesheet-agent-tests.yml` runs this project's offline tests. The workflow inside this project folder is a template for publishing it as a standalone repository; GitHub does not run nested workflow templates. Uploading the code does not start live monitoring.

## Try it without an account or sending email

From this folder:

```powershell
python timesheet_agent.py demo
python -m unittest discover -v
python timesheet_agent.py check --config config.example.json --dry-run
```

The demo observes `Submitted for Approval` twice, then `Approved` twice. It prints one simulated email. It uses temporary state and never accesses a website or sends real email. All example dates and addresses are synthetic.

## What your IT team needs to provide

1. An approved JSON export file or HTTPS JSON endpoint containing your timesheet summary. The existing Compass landing-page URL is an HTML/SSO page, not a JSON API endpoint. A file works only if an approved process keeps it up to date; manually exporting it is not automatic Compass monitoring.
2. An approved SMTP server, sender address, and supported authentication. This implementation supports a TLS SMTP relay with optional username/password authentication. If your organization requires OAuth or another mail service, its approved mail adapter still needs implementation. Do not enable basic authentication or bypass company browser restrictions to use this project.

An approved producer should generate a fresh snapshot on each check, with this schema:

```json
{
  "demo": false,
  "generated_at": "2026-01-09T18:00:00Z",
  "timesheets": [
    {
      "from_date": "2026-01-03",
      "to_date": "2026-01-09",
      "status": "Submitted for Approval"
    }
  ]
}
```

`generated_at` must be the actual collection time, include a timezone, and be within `max_age_seconds` (default two hours). The sample timestamp above must be replaced. Preserve the portal's exact status text. Empty, malformed, conflicting, stale, or backwards data fails the check and does not advance the last saved status. The most recent period is selected by end date, regardless of row order.

## Configure live use after IT approval

1. Copy `config.example.json` to `config.json` (excluded from Git).
2. Set `recipient` to your company email. Set `smtp.from_address` to the sender authorized by your mail administrator.
3. Set `source.path` to your approved JSON file. Paths are relative to the configuration file. For an approved HTTPS endpoint, replace `source` with:

   ```json
   {
     "kind": "https",
     "url": "https://approved-api.company.example/timesheets",
     "bearer_token_env": "TIMESHEET_FEED_TOKEN"
   }
   ```

   Omit `bearer_token_env` if the approved endpoint does not require it. HTTPS redirects and HTML sign-in pages are rejected. Do not put tokens in URLs or JSON configuration.
4. Configure the approved SMTP host, port, `security` (`starttls` or `ssl`), and optional username. Remove `username` for an approved relay that requires no login. Set `portal_url` to the actual timesheet landing page.
5. Supply secrets through environment variables in the running process, using your organization's secret-management method. The example expects `TIMESHEET_SMTP_PASSWORD` and, when configured, `TIMESHEET_FEED_TOKEN`. The program does not read `.env` files. Do not share passwords in chat or commit them to GitHub.
6. Set `enabled` to `true` only when the approved source and email settings are ready.
7. Run the following commands:

   ```powershell
   python timesheet_agent.py check --config config.json --dry-run
   python timesheet_agent.py test-email --config config.json
   python timesheet_agent.py check --config config.json
   python timesheet_agent.py watch --config config.json
   ```

`test-email` sends a clearly labeled test without reading timesheet data or changing state. SMTP acceptance is not proof of inbox delivery; check the recipient inbox. `watch` checks immediately, then every 3,600 seconds after each check completes. Keep the process running; Ctrl+C stops it. Configuration is read at startup, so restart after changing it. This project does not install a background Windows service or enable any hosted schedule.

## Notification behavior

- First live observation: save the current period and status as a baseline, with no email.
- Same period and same status: no email.
- Status changes, including rejection or resubmission: send one email with dates, previous status, new status, portal link, and UTC observation time.
- A newer period appears: send one initial status email for that period and track it.
- Send failure: leave the saved status unchanged and retry on the next check.
- Preview with `--dry-run`: no email, no state creation or changes. A preview does not validate live freshness, credentials, or SMTP connectivity.

Only the latest period is monitored. Changes to older periods are not reported after a newer one appears. Hourly polling can miss intermediate statuses that change between checks.

SQLite stores local state in `data/state.sqlite3`. Keep that file between runs; deleting it resets the baseline. It serializes checks on a single local database. Use one state database per recipient/source and keep it on a local disk with reliable locking, outside a sync folder for production. There is a small duplicate-delivery risk if SMTP accepts an email and the process crashes before committing state, or the connection fails after acceptance. SMTP cannot guarantee exactly-once inbox delivery.

## Files

| File | Purpose |
| --- | --- |
| `timesheet_agent.py` | Feed validation, latest-period selection, state, email, and CLI |
| `test_timesheet_agent.py` | Offline behavior and mail-adapter tests |
| `config.example.json` | Disabled configuration template |
| `examples/timesheets.demo.json` | Synthetic sample data; rejected for live checks |
| `publish_github.py` | Creates a private repository and uploads this source using your own authorized token |
| `.github/workflows/tests.yml` | Runs offline tests on pushes and pull requests |

## GitHub upload

The GitHub connection available in the original chat can edit existing repositories but has no create-repository action. No GitHub repository has been created just by generating these files.

If you have an approved local Python installation and a GitHub token with repository-creation permission, supply it through the `GH_TOKEN` environment variable and run:

```powershell
python publish_github.py
```

The helper creates a **private** repository named `timesheet-status-agent` in the token owner's account. It uploads only the source files listed in the helper, in one commit. It excludes local configuration, actual timesheet data, state, screenshots, credentials, and archives. If the repository already exists, it stops without overwriting anything. Never paste your token in chat.

Alternatively, create a private repository at https://github.com/new and upload the source files. Creating/uploading the repository does not activate monitoring or grant access to Compass.
