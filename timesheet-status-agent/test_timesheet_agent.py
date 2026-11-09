import io
import json
import os
from pathlib import Path
import smtplib
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import timesheet_agent as agent


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / 'state.sqlite3'
        self.submitted = agent.Timesheet('2026-01-03', '2026-01-09', 'Submitted for Approval')
        self.approved = agent.Timesheet('2026-01-03', '2026-01-09', 'Approved')
        self.sender = Mock()

    def observe(self, item, **kwargs):
        return agent.record_observation(self.state, item, self.sender, **kwargs)

    def test_baseline_is_silent(self):
        self.assertIn('Baseline saved', self.observe(self.submitted))
        self.sender.assert_not_called()

    def test_change_sends_once_even_after_restart(self):
        self.observe(self.submitted)
        self.observe(self.approved)
        self.observe(self.approved)
        self.sender.assert_called_once_with(self.approved, self.submitted)

    def test_failed_delivery_retries_without_losing_change(self):
        self.observe(self.submitted)
        self.sender.side_effect = agent.AgentError('Delivery failed')
        with self.assertRaises(agent.AgentError):
            self.observe(self.approved)
        self.sender.side_effect = None
        self.observe(self.approved)
        self.observe(self.approved)
        self.assertEqual(self.sender.call_count, 2)

    def test_new_period_alerts_once(self):
        self.observe(self.approved)
        newer = agent.Timesheet('2026-01-10', '2026-01-16', 'Submitted for Approval')
        self.observe(newer)
        self.observe(newer)
        self.sender.assert_called_once_with(newer, None)

    def test_backwards_feed_does_not_overwrite_state(self):
        self.observe(self.approved)
        with self.assertRaises(agent.AgentError):
            self.observe(agent.Timesheet('2025-12-27', '2026-01-02', 'Approved'))
        self.observe(self.approved)
        self.sender.assert_not_called()

    def test_dry_run_creates_no_database(self):
        self.observe(self.submitted, dry_run=True)
        self.assertFalse(self.state.exists())
        self.sender.assert_not_called()

    def test_dry_run_does_not_consume_change(self):
        self.observe(self.submitted)
        self.assertIn('Would send', self.observe(self.approved, dry_run=True))
        self.sender.assert_not_called()
        self.observe(self.approved)
        self.sender.assert_called_once()

    def test_rejection_and_resubmission_are_distinct_changes(self):
        self.observe(self.submitted)
        rejected = agent.Timesheet('2026-01-03', '2026-01-09', 'Rejected')
        self.observe(rejected)
        self.observe(self.submitted)
        self.assertEqual(self.sender.call_count, 2)


class FeedTests(unittest.TestCase):
    def payload(self, **changes):
        return {'demo': False, 'generated_at': datetime.now(timezone.utc).isoformat(),
                'timesheets': [{'from_date': '2026-01-03', 'to_date': '2026-01-09', 'status': 'Submitted for Approval'}], **changes}

    def test_selects_latest_end_date_in_unsorted_feed(self):
        payload = self.payload()
        payload['timesheets'].append({'from_date': '2025-12-27', 'to_date': '2026-01-02', 'status': 'Approved'})
        self.assertEqual(agent.latest_timesheet(payload).to_date, '2026-01-09')

    def test_invalid_dates_empty_status_and_conflicting_rows_fail(self):
        cases = [[], [{'from_date': 'bad', 'to_date': '2026-01-09', 'status': 'Approved'}],
                 [{'from_date': '2026-01-10', 'to_date': '2026-01-09', 'status': 'Approved'}],
                 [{'from_date': '2026-01-03', 'to_date': '2026-01-09', 'status': ' '}]]
        for rows in cases:
            with self.subTest(rows=rows), self.assertRaises(agent.AgentError):
                agent.latest_timesheet(self.payload(timesheets=rows))
        payload = self.payload()
        payload['timesheets'].append({**payload['timesheets'][0], 'status': 'Approved'})
        with self.assertRaises(agent.AgentError):
            agent.latest_timesheet(payload)

    def test_live_rejects_demo_stale_future_or_untimed_snapshots(self):
        now = datetime.now(timezone.utc)
        cases = [{'demo': True}, {'generated_at': (now - timedelta(hours=3)).isoformat()},
                 {'generated_at': (now + timedelta(hours=1)).isoformat()}, {'generated_at': '2026-01-09T12:00:00'}]
        for change in cases:
            with self.subTest(change=change), self.assertRaises(agent.AgentError):
                agent.latest_timesheet(self.payload(**change), live=True)

    def test_fresh_live_snapshot_preserves_exact_status(self):
        self.assertEqual(agent.latest_timesheet(self.payload(), live=True).status, 'Submitted for Approval')

    def test_redirect_is_rejected(self):
        with self.assertRaises(agent.AgentError):
            agent.NoRedirects().redirect_request(None, None, 302, 'Found', {}, 'https://other.example')

    def test_plain_http_is_rejected_before_network(self):
        with self.assertRaises(agent.AgentError):
            agent.read_source({'source': {'kind': 'https', 'url': 'http://example.com/feed'}})


class MailTests(unittest.TestCase):
    def config(self):
        return {'recipient': 'recipient@company.example',
                'smtp': {'host': 'smtp.company.example', 'from_address': 'sender@company.example', 'security': 'starttls'}}

    def test_subject_and_body_include_range_exact_status_and_previous(self):
        message = agent.alert_message(self.config(), agent.Timesheet('2026-01-03', '2026-01-09', 'Approved'),
                                      agent.Timesheet('2026-01-03', '2026-01-09', 'Submitted for Approval'))
        for value in ['2026-01-03', '2026-01-09', 'Approved']:
            self.assertIn(value, message['Subject'])
            self.assertIn(value, message.get_content())
        self.assertIn('Previous status: Submitted for Approval', message.get_content())

    def test_test_email_does_not_claim_live_access(self):
        message = agent.alert_message(self.config(), None, test=True)
        self.assertEqual(message['Subject'], 'Timesheet alert test')
        self.assertIn('No live timesheet status was checked', message.get_content())

    @patch('timesheet_agent.smtplib.SMTP')
    def test_starttls_and_smtp_acceptance(self, factory):
        client = factory.return_value.__enter__.return_value
        client.send_message.return_value = {}
        message = agent.alert_message(self.config(), None, test=True)
        agent.smtp_send(self.config(), message)
        client.starttls.assert_called_once()
        client.send_message.assert_called_once_with(message)

    @patch('timesheet_agent.smtplib.SMTP_SSL')
    def test_implicit_tls(self, factory):
        config = self.config()
        config['smtp']['security'] = 'ssl'
        factory.return_value.__enter__.return_value.send_message.return_value = {}
        agent.smtp_send(config, agent.alert_message(config, None, test=True))
        self.assertEqual(factory.call_args.args[1], 465)

    @patch('timesheet_agent.smtplib.SMTP')
    def test_smtp_failure_is_reported_without_server_response(self, factory):
        factory.side_effect = smtplib.SMTPException('private server diagnostic')
        with self.assertRaises(agent.AgentError) as caught:
            agent.smtp_send(self.config(), agent.alert_message(self.config(), None, test=True))
        self.assertNotIn('private server diagnostic', str(caught.exception))

    @patch.dict(os.environ, {}, clear=True)
    @patch('timesheet_agent.smtplib.SMTP')
    def test_missing_password_never_connects(self, factory):
        config = self.config()
        config['smtp']['username'] = 'sender@company.example'
        with self.assertRaises(agent.AgentError):
            agent.smtp_send(config, agent.alert_message(config, None, test=True))
        factory.assert_not_called()

    def test_disabled_config_cannot_send(self):
        with self.assertRaises(agent.AgentError):
            agent.ensure_enabled({'enabled': False})

    def test_header_injection_rejected(self):
        config = self.config()
        config['recipient'] = 'person@example.com\nBcc:other@example.com'
        with self.assertRaises(agent.AgentError):
            agent.alert_message(config, None, test=True)


class IntegrationTests(unittest.TestCase):
    def test_file_feed_baseline_change_and_duplicate(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            feed_path = root / 'feed.json'
            config_path = root / 'config.json'
            config_path.write_text(json.dumps({'enabled': True, 'recipient': 'recipient@company.example',
                'source': {'kind': 'file', 'path': 'feed.json'},
                'smtp': {'from_address': 'sender@company.example'}}), encoding='utf-8')
            config = agent.load_config(config_path)
            payload = FeedTests().payload()
            feed_path.write_text(json.dumps(payload), encoding='utf-8')
            with patch('timesheet_agent.smtp_send') as sender:
                self.assertIn('Baseline saved', agent.check(config))
                payload['timesheets'][0]['status'] = 'Approved'
                feed_path.write_text(json.dumps(payload), encoding='utf-8')
                agent.check(config)
                agent.check(config)
                sender.assert_called_once()
                self.assertIn('Approved', sender.call_args.args[1]['Subject'])

    def test_demo_is_offline(self):
        output = io.StringIO()
        with redirect_stdout(output), patch('timesheet_agent.smtp_send') as sender:
            agent.demo()
        sender.assert_not_called()
        self.assertIn('1 simulated email', output.getvalue())


if __name__ == '__main__':
    unittest.main()
