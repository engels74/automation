"""Compute an allowlisted Biome repair without write credentials; publish via Git data APIs."""

import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BOT = 'renovate[bot]'
EXTENSIONS = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.css', '.json', '.jsonc', '.astro', '.svelte'}
PROTECTED = {'package.json', 'bun.lock', 'bun.lockb', 'package-lock.json', 'renovate.json', 'renovate.jsonc', 'prek.toml'}


def lines(value):
    return [line.strip() for line in value.splitlines() if line.strip()]


def relative(value):
    path = PurePosixPath(value)
    if path.is_absolute() or '..' in path.parts or not path.parts or any(p.startswith('.') for p in path.parts):
        raise ValueError(f'unsafe relative path: {value}')
    return path


def allowed(path, configs, roots):
    try:
        parsed = relative(path)
    except ValueError:
        return False
    if path in configs:
        return parsed.name in {'biome.json', 'biome.jsonc'}
    if parsed.name in PROTECTED or parsed.suffix not in EXTENSIONS:
        return False
    return any(parsed == relative(root) or relative(root) in parsed.parents for root in roots)


def eligible(pr, repository, expected_head):
    return (
        pr.get('state') == 'open'
        and not pr.get('draft')
        and pr.get('user', {}).get('login') == BOT
        and pr.get('user', {}).get('id') == 29139614
        and pr.get('user', {}).get('type') == 'Bot'
        and pr.get('head', {}).get('repo', {}).get('full_name') == repository
        and pr.get('base', {}).get('repo', {}).get('full_name') == repository
        and pr.get('head', {}).get('sha') == expected_head
        and bool(re.fullmatch(r'[0-9a-f]{40}', expected_head))
    )


class GitHub:
    def __init__(self, repository):
        self.base = f'https://api.github.com/repos/{repository}'

    def call(self, path, data=None, method=None):
        payload = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.base + path, data=payload, method=method,
            headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                     'Content-Type': 'application/json',
                     'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
        with urllib.request.urlopen(request, timeout=60) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def dispatch(self, workflow, branch, pr_number, head):
        data = {'ref': branch, 'inputs': {'pr-number': str(pr_number), 'expected-head-sha': head}}
        for attempt in range(3):
            try:
                return self.call(f'/actions/workflows/{workflow}/dispatches', data)
            except urllib.error.HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
            except urllib.error.URLError:
                if attempt == 2:
                    raise
            time.sleep(2 ** attempt)


def validate_payload(payload, configs, roots):
    files = payload.get('files')
    if not isinstance(files, list) or not 1 <= len(files) <= 200:
        raise ValueError('repair must change between one and 200 existing files')
    seen = set()
    for item in files:
        path = item['path']
        if path in seen or not allowed(path, configs, roots):
            raise ValueError(f'repair path is not allowed: {path}')
        seen.add(path)
        raw = base64.b64decode(item['content'], validate=True)
        if len(raw) > 2_000_000 or b'\0' in raw:
            raise ValueError(f'invalid repair content: {path}')
        raw.decode('utf-8')
    return files


def compute(configs, roots, package_directory, api, pr_number, head, output):
    repository = os.environ['GITHUB_REPOSITORY']
    pr = api.call(f'/pulls/{pr_number}')
    if not eligible(pr, repository, head):
        raise ValueError('not an eligible current same-repository Renovate PR')
    if not pr['head']['ref'].startswith('renovate/biome'):
        raise ValueError('repair requires the dedicated Renovate Biome group')
    # A package update must be present; ordinary user PRs and standalone config edits are ineligible.
    changed = []
    page = 1
    while True:
        batch = api.call(f'/pulls/{pr_number}/files?per_page=100&page={page}')
        changed.extend(item['filename'] for item in batch)
        if len(batch) < 100:
            break
        page += 1
        if page > 30:
            raise ValueError('PR too large to inspect completely')
    package = Path(package_directory) / 'package.json'
    manifest = json.loads(package.read_text())
    deps = {**manifest.get('dependencies', {}), **manifest.get('devDependencies', {})}
    if '@biomejs/biome' not in deps or not any(Path(p).name in {'package.json', 'bun.lock', 'bun.lockb'} for p in changed):
        raise ValueError('no installed Biome update surface found')
    # Neither dependency installation nor the formatter receives the API token.
    tool_env = {key: value for key, value in os.environ.items() if key in {'PATH', 'HOME', 'TMPDIR', 'LANG', 'SYSTEMROOT'}}
    subprocess.run(['bun', 'install', '--frozen-lockfile', '--ignore-scripts'], cwd=package_directory, env=tool_env, check=True)
    installed = json.loads((Path(package_directory) / 'node_modules/@biomejs/biome/package.json').read_text())
    version = installed.get('version', '')
    if installed.get('name') != '@biomejs/biome' or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('repair requires an official stable Biome package version')
    # Install the official package outside the PR tree; never execute its .bin shim.
    with tempfile.TemporaryDirectory(prefix="biome-tool-") as tool_directory:
        subprocess.run(['bun', 'add', '--exact', '--ignore-scripts', '--registry=https://registry.npmjs.org', f'@biomejs/biome@{version}'], cwd=tool_directory, env=tool_env, check=True)
        binary = (Path(tool_directory) / 'node_modules/.bin/biome').resolve(strict=True)
        tracked = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
        sources = [p for p in tracked if p and p not in configs and allowed(p, configs, roots)]
        for file in configs + sources:
            if Path(file).is_symlink() or not Path(file).is_file():
                raise ValueError(f'repair requires regular tracked files: {file}')

        def fix():
            for config in configs:
                subprocess.run([str(binary), 'migrate', '--write', '--config-path', config], env=tool_env, check=True)
                subprocess.run([str(binary), 'check', '--write', '--config-path', config, config], env=tool_env, check=True)
                owner = PurePosixPath(config).parent
                local = [p for p in sources if (owner == PurePosixPath('.') or owner in PurePosixPath(p).parents)
                         and not any(PurePosixPath(other).parent != owner and PurePosixPath(other).parent in PurePosixPath(p).parents
                                     and owner in PurePosixPath(other).parents for other in configs)]
                if local:
                    subprocess.run([str(binary), 'check', '--write', '--no-errors-on-unmatched', '--config-path', config, *local], env=tool_env, check=True)

        fix()
        before = {p: Path(p).read_bytes() for p in configs + sources}
        fix()
        if any(Path(p).read_bytes() != content for p, content in before.items()):
            raise ValueError('Biome repair is not idempotent')
        diff = subprocess.check_output(['git', 'diff', '--name-only', '-z', 'HEAD']).decode().split('\0')
        files = [{'path': p, 'content': base64.b64encode(Path(p).read_bytes()).decode()} for p in diff if p]
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as stream:
            stream.write(f'changed={str(bool(files)).lower()}\n')
        if not files:
            return
        payload = {'head': head, 'pr': pr_number, 'repository': repository, 'files': files}
        validate_payload(payload, configs, roots)
        Path(output).write_text(json.dumps(payload))


def publish(payload, configs, roots, api, repository, workflow, expected_pr, expected_head):
    if payload.get('repository') != repository or payload.get('pr') != expected_pr or payload.get('head') != expected_head:
        raise ValueError('artifact does not match the triggering PR/head')
    files = validate_payload(payload, configs, roots)
    pr = api.call(f'/pulls/{expected_pr}')
    if not eligible(pr, repository, expected_head) or not pr['head']['ref'].startswith('renovate/biome'):
        raise ValueError('PR changed or is no longer eligible')
    commit = api.call(f'/git/commits/{expected_head}')
    old_tree = api.call('/git/trees/' + commit['tree']['sha'] + '?recursive=1')
    if old_tree.get('truncated'):
        raise ValueError('cannot validate truncated repository tree')
    entries = {entry['path']: entry for entry in old_tree['tree']}
    tree = []
    for item in files:
        old = entries.get(item['path'], {})
        if old.get('type') != 'blob' or old.get('mode') not in {'100644', '100755'}:
            raise ValueError('repair cannot add files, symlinks or change modes')
        blob = api.call('/git/blobs', {'content': item['content'], 'encoding': 'base64'})
        tree.append({'path': item['path'], 'mode': old['mode'], 'type': 'blob', 'sha': blob['sha']})
    new_tree = api.call('/git/trees', {'base_tree': commit['tree']['sha'], 'tree': tree})
    new_commit = api.call('/git/commits', {
        'message': 'chore(deps): migrate Biome configuration and formatting\n\nSigned-off-by: github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com>',
        'tree': new_tree['sha'], 'parents': [expected_head],
        'author': {'name': 'github-actions[bot]', 'email': '41898282+github-actions[bot]@users.noreply.github.com'}})
    # A racing push makes this non-fast-forward update fail; never overwrite it.
    api.call('/git/refs/heads/' + pr['head']['ref'], {'sha': new_commit['sha'], 'force': False}, 'PATCH')
    recovery = {'workflow': workflow, 'ref': pr['head']['ref'], 'pr-number': expected_pr, 'expected-head-sha': new_commit['sha']}
    print('Full CI dispatch target (also usable for manual recovery):', json.dumps(recovery))
    api.dispatch(workflow, pr['head']['ref'], expected_pr, new_commit['sha'])
    return new_commit['sha']


def main():
    configs, roots = lines(os.environ['CONFIG_FILES']), lines(os.environ['SOURCE_ROOTS'])
    if not configs or any(not allowed(c, configs, []) for c in configs):
        raise ValueError('explicit Biome config paths are required')
    repository = os.environ['GITHUB_REPOSITORY']
    api = GitHub(repository)
    pr_number = int(os.environ['PR_NUMBER'])
    head = os.environ['EXPECTED_HEAD']
    if sys.argv[1] == 'compute':
        directory = os.environ['PACKAGE_DIRECTORY']
        if directory != '.':
            relative(directory)
        compute(configs, roots, directory, api, pr_number, head, os.environ['REPAIR_FILE'])
    elif sys.argv[1] == 'publish':
        payload = json.loads(Path(os.environ['REPAIR_FILE']).read_text())
        print('Dispatched full CI for repaired commit', publish(payload, configs, roots, api, repository, os.environ['CI_WORKFLOW'], pr_number, head))
    else:
        raise ValueError('unknown repair operation')


if __name__ == '__main__':
    main()
