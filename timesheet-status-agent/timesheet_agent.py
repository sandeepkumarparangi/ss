"""Timesheet status alerts using an approved JSON feed and SMTP. Python 3.11+."""

import argparse
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import ssl
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class AgentError(Exception):
    """A configuration, source, or delivery failure safe to show in the console."""


@dataclass(frozen=True)
class Timesheet:
    from_date: str
    to_date: str
    status: str


def text(value, field):
    if not isinstance(value, str) or not value.strip() or any(c in value for c in '\r\n\x00'):
        raise AgentError(f'{field} must be nonempty, single-line text.')
    return value.strip()


def latest_timesheet(payload, *, live=False, max_age_seconds=7200):
    if not isinstance(payload, dict):
        raise AgentError('Source must return a JSON object, not an HTML sign-in page.')
    if live:
        if payload.get('demo') is not False:
            raise AgentError('Live feeds must explicitly set demo to false.')
        try:
            stamp = datetime.fromisoformat(payload['generated_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                raise ValueError('Timezone required')
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            if age < -300 or age > max_age_seconds:
                raise ValueError('Stale or future snapshot')
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise AgentError('Live snapshot needs a recent generated_at timestamp with timezone.') from exc
    rows = payload.get('timesheets')
    if not isinstance(rows, list) or not rows:
        raise AgentError('Source contains no timesheets; last saved status is unchanged.')
    parsed = {}
    for row in rows:
        try:
            start = text(row['from_date'], 'from_date')
            end = text(row['to_date'], 'to_date')
            if date.fromisoformat(start).isoformat() != start or date.fromisoformat(end).isoformat() != end:
                raise ValueError('Use YYYY-MM-DD')
            if start > end:
                raise ValueError('Reversed date range')
            status = text(row['status'], 'status')
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentError('Each timesheet needs valid YYYY-MM-DD dates and a status.') from exc
        if end in parsed and parsed[end] != Timesheet(start, end, status):
            raise AgentError('Conflicting timesheets share the same end date.')
        parsed[end] = Timesheet(start, end, status)
    return parsed[max(parsed)]


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError) as exc:
        raise AgentError('Cannot read valid JSON from the configured file.') from exc


def relative_path(config, value):
    path = Path(value)
    return path if path.is_absolute() else config['_directory'] / path


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward authorization to another location or follow an SSO page.
        raise AgentError('The feed redirected. Configure the approved JSON endpoint directly.')


def read_source(config):
    source = config['source']
    if source['kind'] == 'file':
        return read_json(relative_path(config, source['path']))
    url = urlsplit(source['url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
        raise AgentError('Feed URL must use HTTPS without embedded credentials or a fragment.')
    headers = {'Accept': 'application/json', 'Cache-Control': 'no-cache'}
    token_env = source.get('bearer_token_env')
    if token_env:
        token = os.environ.get(token_env)
        if not token:
            raise AgentError('The feed token environment variable is missing.')
        headers['Authorization'] = 'Bearer ' + text(token, 'feed token')
    try:
        with build_opener(NoRedirects()).open(Request(source['url'], headers=headers), timeout=30) as response:
            if response.headers.get_content_type() != 'application/json':
                raise AgentError('Feed returned something other than application/json.')
            body = response.read(1024 * 1024 + 1)
            if len(body) > 1024 * 1024:
                raise AgentError('Feed exceeds the 1 MB size limit.')
            return json.loads(body)
    except (URLError, OSError, ValueError) as exc:
        # Avoid logging tokens, response bodies, or credential-bearing URLs.
        raise AgentError('Cannot read the HTTPS JSON feed; check access with IT.') from exc


def load_config(path):
    config = read_json(path)
    if not isinstance(config, dict):
        raise AgentError('Configuration must be a JSON object.')
    config['_directory'] = path.resolve().parent
    if config.get('enabled', False) is not True and config.get('enabled', False) is not False:
        raise AgentError('enabled must be a JSON boolean.')
    source = config.get('source', {})
    if not isinstance(source, dict) or source.get('kind') not in ('file', 'https'):
        raise AgentError('source.kind must be file or https.')
    text(source.get('path' if source['kind'] == 'file' else 'url'), 'source location')
    for key, default, minimum in [('interval_seconds', 3600, 60), ('max_age_seconds', 7200, 1)]:
        value = config.get(key, default)
        if type(value) is not int or value < minimum:
            raise AgentError(f'{key} must be an integer of at least {minimum}.')
        config[key] = value
    text(config.get('state_path', 'data/state.sqlite3'), 'state_path')
    return config


def address(value):
    value = text(value, 'email address')
    if not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', value):
        raise AgentError('Configure a single valid sender and recipient email address.')
    return value


def alert_message(config, current, previous=None, *, test=False):
    smtp = config.get('smtp', {})
    message = EmailMessage()
    message['From'] = address(smtp.get('from_address'))
    message['To'] = address(config.get('recipient'))
    message['Date'] = format_datetime(datetime.now(timezone.utc))
    message['Message-ID'] = make_msgid()
    if test:
        message['Subject'] = 'Timesheet alert test'
        body = 'This is a test of your timesheet notification email. No live timesheet status was checked.'
    else:
        prefix = 'Timesheet status changed' if previous else 'New timesheet period'
        message['Subject'] = f'{prefix}: {current.from_date} to {current.to_date} - {current.status}'
        body = f'Your recent timesheet from {current.from_date} to {current.to_date} is now {current.status}.'
        if previous:
            body += f'\nPrevious status: {previous.status}.'
        body += f'\nObserved at: {datetime.now(timezone.utc).isoformat()} (UTC).'
        if config.get('portal_url'):
            body += '\nTimesheet portal: ' + text(config['portal_url'], 'portal_url')
    message.set_content(body)
    return message


def smtp_send(config, message):
    smtp = config.get('smtp', {})
    host = text(smtp.get('host'), 'smtp.host')
    security = smtp.get('security', 'starttls')
    if security not in ('starttls', 'ssl'):
        raise AgentError('SMTP requires starttls or ssl encryption.')
    port = smtp.get('port', 465 if security == 'ssl' else 587)
    if type(port) is not int or not 1 <= port <= 65535:
        raise AgentError('SMTP port must be between 1 and 65535.')
    username = smtp.get('username')
    password = None
    if username:
        username = text(username, 'smtp.username')
        password = os.environ.get(smtp.get('password_env', 'TIMESHEET_SMTP_PASSWORD'))
        if not password:
            raise AgentError('The SMTP password environment variable is missing.')
    factory = smtplib.SMTP_SSL if security == 'ssl' else smtplib.SMTP
    kwargs = {'timeout': 30}
    if security == 'ssl':
        kwargs['context'] = ssl.create_default_context()
    try:
        with factory(host, port, **kwargs) as client:
            if security == 'starttls':
                client.ehlo()
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
            if username:
                client.login(username, password)
            refused = client.send_message(message)
            if refused:
                raise AgentError('SMTP refused the recipient; saved status was not advanced.')
    except (smtplib.SMTPException, OSError) as exc:
        raise AgentError('SMTP send failed; saved status was not advanced. Check the approved relay with IT.') from exc


def ensure_enabled(config):
    if config.get('enabled') is not True:
        raise AgentError('Sending is disabled. Configure approved access and SMTP, then set enabled to true.')


def record_observation(state_path, current, deliver, *, dry_run=False):
    """Save only after successful delivery; serialize overlapping local checks."""
    if dry_run and not state_path.exists():
        return 'Would save the first live observation as the baseline; no email.'
    if not dry_run:
        state_path.parent.mkdir(parents=True, exist_ok=True)
    target = state_path.resolve().as_uri() + '?mode=ro' if dry_run else str(state_path)
    with closing(sqlite3.connect(target, uri=dry_run, timeout=35)) as db:
        try:
            if not dry_run:
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS latest (id INTEGER PRIMARY KEY CHECK (id=1), from_date TEXT NOT NULL, to_date TEXT NOT NULL, status TEXT NOT NULL)')
            previous_row = db.execute('SELECT from_date, to_date, status FROM latest WHERE id=1').fetchone()
            previous = Timesheet(*previous_row) if previous_row else None
            if previous and (current.to_date < previous.to_date or (current.to_date == previous.to_date and current.from_date != previous.from_date)):
                raise AgentError('Feed moved backwards or changed the period boundaries; saved status is unchanged.')
            changed = previous and current != previous
            if dry_run:
                return 'Would send a status email; no state changed.' if changed else 'No status change; no email.'
            if changed:
                deliver(current, previous if previous.to_date == current.to_date else None)
            db.execute('INSERT OR REPLACE INTO latest VALUES (1, ?, ?, ?)', (current.from_date, current.to_date, current.status))
            db.commit()
            return 'Email accepted by SMTP; status saved.' if changed else ('Baseline saved; no email.' if not previous else 'No status change; no email.')
        except Exception:
            db.rollback()
            raise


def check(config, *, dry_run=False):
    if not dry_run:
        ensure_enabled(config)
    current = latest_timesheet(read_source(config), live=not dry_run, max_age_seconds=config['max_age_seconds'])
    state_path = relative_path(config, config.get('state_path', 'data/state.sqlite3'))
    return record_observation(state_path, current, lambda item, previous: smtp_send(config, alert_message(config, item, previous)), dry_run=dry_run)


def demo():
    """Run the real state logic with synthetic data and a console-only sender."""
    with tempfile.TemporaryDirectory(prefix='timesheet-demo-') as folder:
        state_path = Path(folder) / 'demo.sqlite3'
        delivered = []
        def preview(current, previous):
            delivered.append(current)
            print(f'DEMO EMAIL: Your recent timesheet from {current.from_date} to {current.to_date} is now {current.status}.')
        for status in ['Submitted for Approval', 'Submitted for Approval', 'Approved', 'Approved']:
            print(record_observation(state_path, Timesheet('2026-01-03', '2026-01-09', status), preview))
        print(f'Demo complete: {len(delivered)} simulated email; no network or real email used.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['demo', 'check', 'watch', 'test-email'])
    parser.add_argument('--config', type=Path, default=Path('config.json'))
    parser.add_argument('--dry-run', action='store_true', help='Preview a check without email or state changes.')
    args = parser.parse_args()
    try:
        if args.command == 'demo':
            demo()
            return 0
        if args.dry_run and args.command != 'check':
            raise AgentError('--dry-run is supported only with check.')
        config = load_config(args.config)
        if args.command == 'test-email':
            ensure_enabled(config)
            smtp_send(config, alert_message(config, None, test=True))
            print('Test email accepted by SMTP. Check the recipient inbox and junk folder.')
        elif args.command == 'check':
            print(check(config, dry_run=args.dry_run))
        else:
            ensure_enabled(config)
            while True:
                try:
                    print(check(config), flush=True)
                except (AgentError, sqlite3.Error) as exc:
                    print(f'Check failed ({type(exc).__name__}); no successful delivery recorded.', file=sys.stderr, flush=True)
                time.sleep(config['interval_seconds'])
        return 0
    except (AgentError, sqlite3.Error) as exc:
        print(str(exc) if isinstance(exc, AgentError) else 'State database is unavailable; check file access.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
