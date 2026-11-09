"""Create a private GitHub repository and upload this source, using GH_TOKEN."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener

from timesheet_agent import NoRedirects


FILES = (
    '.gitignore',
    '.github/workflows/tests.yml',
    'README.md',
    'config.example.json',
    'examples/timesheets.demo.json',
    'timesheet_agent.py',
    'test_timesheet_agent.py',
    'publish_github.py',
    'test_publish_github.py',
)


class PublishError(Exception):
    pass


def api(token, method, endpoint, payload=None):
    request = Request('https://api.github.com' + endpoint,
                      data=json.dumps(payload).encode('utf-8') if payload is not None else None,
                      headers={'Authorization': 'Bearer ' + token,
                               'Accept': 'application/vnd.github+json',
                               'Content-Type': 'application/json',
                               'User-Agent': 'timesheet-status-agent',
                               'X-GitHub-Api-Version': '2022-11-28'}, method=method)
    try:
        with build_opener(NoRedirects()).open(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code == 404 and method == 'GET':
            return None
        raise PublishError(f'GitHub returned HTTP {exc.code}. Check token permissions; no force update was used.') from exc
    except (URLError, OSError, ValueError) as exc:
        raise PublishError('GitHub request failed. Check your account before retrying: repository creation may have succeeded.') from exc


def publish(root, name, token):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,99}', name):
        raise PublishError('Choose a repository name with letters, numbers, dots, hyphens, or underscores.')
    # Read only known source files, never an arbitrary recursive folder upload.
    entries = []
    for filename in FILES:
        path = root / filename
        if path.is_symlink() or not path.is_file():
            raise PublishError(f'Missing or unsafe source file: {filename}')
        entries.append({'path': filename, 'mode': '100644', 'type': 'blob', 'content': path.read_text(encoding='utf-8')})
    profile = api(token, 'GET', '/user')
    if not profile or not profile.get('login'):
        raise PublishError('Could not identify the GitHub account.')
    repository = profile['login'] + '/' + name
    base = '/repos/' + repository
    if api(token, 'GET', base) is not None:
        raise PublishError(f'{repository} already exists. Stopped without changing it.')
    repo = api(token, 'POST', '/user/repos', {
        'name': name, 'private': True, 'auto_init': True,
        'description': 'Timesheet status monitor with approved JSON feeds and email alerts',
    })
    if not repo.get('private') or repo.get('full_name', '').lower() != repository.lower():
        raise PublishError('Unexpected repository response. Stopped before uploading source.')
    print('Created private repository: ' + repo['html_url'], flush=True)
    branch = repo['default_branch']
    ref_path = '/git/ref/heads/' + quote(branch, safe='')
    head = None
    for attempt in range(5):
        head = api(token, 'GET', base + ref_path)
        if head:
            break
        time.sleep(1)
    if not head:
        raise PublishError('Repository was created but its initial branch is not ready. No source was uploaded.')
    parent = head['object']['sha']
    commit = api(token, 'GET', base + '/git/commits/' + parent)
    tree = api(token, 'POST', base + '/git/trees', {'base_tree': commit['tree']['sha'], 'tree': entries})
    new_commit = api(token, 'POST', base + '/git/commits', {
        'message': 'Add timesheet status monitor, offline tests, and setup guide',
        'tree': tree['sha'], 'parents': [parent],
    })
    latest = api(token, 'GET', base + ref_path)
    if latest['object']['sha'] != parent:
        raise PublishError('The branch changed during upload. Stopped without moving the branch.')
    api(token, 'PATCH', base + '/git/refs/heads/' + quote(branch, safe=''), {'sha': new_commit['sha'], 'force': False})
    verified = api(token, 'GET', base + ref_path)
    if verified['object']['sha'] != new_commit['sha']:
        raise PublishError('Upload could not be verified. Inspect the repository before retrying.')
    print(f'Uploaded {len(FILES)} source files: ' + repo['html_url'])
    return repo['html_url']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--name', default='timesheet-status-agent')
    args = parser.parse_args()
    token = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if not token:
        print('Not uploaded: GH_TOKEN is missing. Supply your own authorized token through the environment, never in chat.', file=sys.stderr)
        return 1
    try:
        publish(Path(__file__).resolve().parent, args.name, token)
        return 0
    except (PublishError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
