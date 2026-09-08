"""Fail closed on missing, extra or unsuccessful required GitHub jobs."""

import json
import os
import sys
from pathlib import Path


def failures(needs, required):
    if not isinstance(needs, dict):
        return ['needs must be a JSON object']
    if not required or len(required) != len(set(required)):
        return ['required job IDs must be nonempty and unique']
    errors = []
    for job in sorted(set(required) - needs.keys()):
        errors.append(f'{job}: missing')
    for job in sorted(needs.keys() - set(required)):
        errors.append(f'{job}: undeclared prerequisite')
    for job in required:
        if job in needs:
            result = needs[job].get('result') if isinstance(needs[job], dict) else None
            if result != 'success':
                errors.append(f'{job}: {result or "missing result"}')
    return errors


def main():
    try:
        errors = failures(json.loads(os.environ['CI_NEEDS']), os.environ['CI_REQUIRED'].split())
    except (KeyError, ValueError) as exc:
        errors = [f'invalid gate input: {exc}']
    report = '## Required CI\n\n' + ('\n'.join(f'- {e}' for e in errors) if errors else 'Every required job succeeded.') + '\n'
    print(report)
    if path := os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(path).open('a') as stream:
            stream.write(report)
    return bool(errors)


if __name__ == '__main__':
    sys.exit(main())
