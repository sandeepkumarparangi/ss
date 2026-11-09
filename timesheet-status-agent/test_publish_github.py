from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import publish_github as publisher


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in publisher.FILES:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('source', encoding='utf-8')

    @patch('publish_github.api')
    def test_existing_repository_is_never_overwritten(self, api):
        api.side_effect = [{'login': 'example'}, {'private': True}]
        with self.assertRaises(publisher.PublishError):
            publisher.publish(self.root, 'timesheet-status-agent', 'not-a-real-token')
        self.assertTrue(all(call.args[1] == 'GET' for call in api.call_args_list))

    @patch('publish_github.api')
    def test_upload_is_private_and_excludes_local_configuration(self, api):
        (self.root / 'config.json').write_text('private settings', encoding='utf-8')
        api.side_effect = [
            {'login': 'example'}, None,
            {'private': True, 'full_name': 'example/timesheet-status-agent', 'default_branch': 'main', 'html_url': 'https://github.com/example/timesheet-status-agent'},
            {'object': {'sha': 'parent'}}, {'tree': {'sha': 'base-tree'}},
            {'sha': 'new-tree'}, {'sha': 'new-commit'},
            {'object': {'sha': 'parent'}}, {}, {'object': {'sha': 'new-commit'}},
        ]
        with redirect_stdout(io.StringIO()):
            publisher.publish(self.root, 'timesheet-status-agent', 'not-a-real-token')
        create = api.call_args_list[2]
        self.assertTrue(create.args[3]['private'])
        tree = api.call_args_list[5].args[3]['tree']
        self.assertEqual({entry['path'] for entry in tree}, set(publisher.FILES))
        self.assertNotIn('config.json', {entry['path'] for entry in tree})
        self.assertFalse(api.call_args_list[8].args[3]['force'])

    @patch('publish_github.api')
    def test_missing_source_stops_before_any_github_request(self, api):
        (self.root / 'README.md').unlink()
        with self.assertRaises(publisher.PublishError):
            publisher.publish(self.root, 'timesheet-status-agent', 'not-a-real-token')
        api.assert_not_called()


if __name__ == '__main__':
    unittest.main()
