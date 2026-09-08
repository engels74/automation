import json
import os
import re
import urllib.request
import urllib.parse


def validate(pr, repository, sha, branch, expected):
    return bool(re.fullmatch(r'[0-9a-f]{40}', expected)) and (
        sha == expected == pr.get('head', {}).get('sha')
        and pr.get('state') == 'open'
        and pr.get('head', {}).get('repo', {}).get('full_name') == repository
        and pr.get('base', {}).get('repo', {}).get('full_name') == repository
        and pr.get('head', {}).get('ref') == branch
    )


def main():
    number, expected = os.environ.get('PR_NUMBER', ''), os.environ.get('EXPECTED_HEAD', '')
    default = os.environ.get('EXPECTED_DEFAULT', '')
    if default:
        if number or expected or os.environ['GITHUB_EVENT_NAME'] != 'workflow_dispatch' or not re.fullmatch(r'[0-9a-f]{40}', default):
            raise ValueError('invalid default-branch dispatch inputs')
        repository = os.environ['GITHUB_REPOSITORY']
        def read(path):
            request = urllib.request.Request(f'https://api.github.com/repos/{repository}{path}',
                headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json'})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        branch = read('')['default_branch']
        current = read('/git/ref/heads/' + urllib.parse.quote(branch, safe=''))['object']['sha']
        if not (os.environ['GITHUB_SHA'] == current == default and os.environ['GITHUB_REF_NAME'] == branch):
            raise ValueError('default-branch dispatch is stale or targets the wrong branch')
        return
    if not number and not expected:
        return
    if os.environ['GITHUB_EVENT_NAME'] != 'workflow_dispatch' or not number.isdecimal():
        raise ValueError('invalid repair dispatch inputs')
    repository = os.environ['GITHUB_REPOSITORY']
    request = urllib.request.Request(f'https://api.github.com/repos/{repository}/pulls/{number}',
        headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'], 'Accept': 'application/vnd.github+json'})
    with urllib.request.urlopen(request, timeout=60) as response:
        pr = json.load(response)
    if not validate(pr, repository, os.environ['GITHUB_SHA'], os.environ['GITHUB_REF_NAME'], expected):
        raise ValueError('dispatched CI does not match the current open PR head')


if __name__ == '__main__':
    main()
