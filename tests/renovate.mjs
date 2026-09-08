import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { extractPackageFile } from '../node_modules/renovate/dist/modules/manager/custom/jsonata/index.js';
import { applyPackageRules } from '../node_modules/renovate/dist/util/package-rules/index.js';
import { mergeChildConfig } from '../node_modules/renovate/dist/config/utils.js';
import { resolveConfigPresets } from '../node_modules/renovate/dist/config/presets/index.js';
import { extractPackageFile as extractRegex } from '../node_modules/renovate/dist/modules/manager/custom/regex/index.js';
import { extractPackageFile as extractPreset } from '../node_modules/renovate/dist/modules/manager/renovate-config/extract.js';
import { extractPackageFile as extractActions } from '../node_modules/renovate/dist/modules/manager/github-actions/extract.js';
import { GlobalConfig } from '../node_modules/renovate/dist/config/global.js';

GlobalConfig.set({ localDir: process.cwd() });

const read = async (name) => JSON.parse(await readFile(new URL(`../${name}.json`, import.meta.url), 'utf8'));
const { config: base } = await resolveConfigPresets(await read('default'));
const ready = mergeChildConfig(base, await read('automerge'));
const runtimeManager = base.customManagers.find((m) => m.matchStrings.some((s) => s.startsWith('# renovate:')));
const runtime = extractRegex('      # renovate: datasource=npm depName=bun\n      version: 1.4.2\n', '.github/workflows/ci.yml', runtimeManager);
assert.equal(runtime.deps[0].depName, 'bun');
assert.equal(runtime.deps[0].currentValue, '1.4.2');
const prek = extractRegex('          # renovate: datasource=github-releases depName=j178/prek\n          prek-version: 0.5.2\n', '.github/workflows/code-quality.yml', runtimeManager);
assert.equal(prek.deps[0].depName, 'j178/prek');
assert.equal(prek.deps[0].currentValue, '0.5.2');
const preset = extractPreset(JSON.stringify({ extends: ['github>engels74/automation//default.json#v1.0.1'] }), 'renovate.json');
assert.equal(preset.deps[0].currentValue, 'v1.0.1');
assert.equal(preset.deps[0].depName, 'engels74/automation');
const actions = await extractActions('jobs:\n  check:\n    uses: engels74/automation/.github/workflows/bun.yml@v1.0.1\n', '.github/workflows/ci.yml');
assert.equal(actions.deps[0].currentValue, 'v1.0.1');
assert.equal(actions.deps[0].depName, 'engels74/automation');
const hook = base.customManagers.find((m) => m.fileFormat === 'toml');
const extraction = await extractPackageFile(`
[[repos]]
repo = "builtin"
[[repos.hooks]]
id = "check-json"
[[repos]]
repo = "https://github.com/gitleaks/gitleaks"
rev = "v8.28.0"
[[repos.hooks]]
id = "gitleaks"
[[repos]]
repo = "local"
`, 'prek.toml', hook);
assert.deepEqual(extraction.deps.map(({ depName, currentValue, datasource }) => ({ depName, currentValue, datasource })), [
  { depName: 'gitleaks/gitleaks', currentValue: 'v8.28.0', datasource: 'github-tags' },
]);
const biome = base.customManagers.find((m) => m.depNameTemplate === '@biomejs/biome');
for (const path of ['biome.json', 'frontend/biome.json', 'packages/ui/biome.jsonc']) {
  const extracted = await extractPackageFile('{"$schema":"https://biomejs.dev/schemas/2.5.10/schema.json"}', path, biome);
  assert.equal(extracted.deps[0].depName, '@biomejs/biome');
  assert.equal(extracted.deps[0].currentValue, '2.5.10');
}
async function policy(config, overrides = {}) {
  return applyPackageRules({ ...config, manager: 'bun', datasource: 'npm', depName: 'example', packageName: 'example', currentVersion: '1.0.0', currentValue: '1.0.0', updateType: 'patch', versioning: 'semver', ...overrides });
}
assert.equal((await policy(base)).automerge, false);
assert.equal((await policy(ready)).automerge, true);
assert.equal((await policy(ready, { updateType: 'major' })).automerge, false);
assert.equal((await policy(ready, { currentVersion: '0.4.0', updateType: 'minor' })).dependencyDashboardApproval, true);
assert.equal((await policy(ready, { manager: 'custom.regex', datasource: 'github-releases', packageName: 'j178/prek', depName: 'j178/prek', currentValue: '0.4.0', currentVersion: 'v0.4.0', updateType: 'minor' })).dependencyDashboardApproval, true);
assert.equal((await policy(ready, { packageName: 'typescript', depName: 'typescript', updateType: 'major' })).dependencyDashboardApproval, true);
assert.equal((await policy(ready, { manager: 'github-actions', datasource: 'github-tags', packageName: 'actions/checkout', depName: 'actions/checkout', updateType: 'major' })).pinDigests, false);
assert.equal((await policy(ready, { manager: 'renovate-config', packageName: 'engels74/automation', depName: 'engels74/automation' })).automerge, false);
assert.equal((await policy(ready, { packageName: '@biomejs/biome', depName: '@biomejs/biome' })).groupName, 'biome');
console.log('Renovate extraction and resolved-policy scenarios passed.');

assert.equal(base.platformAutomerge, false);
assert.equal(base.automergeType, 'pr-comment');
assert.equal(base.automergeComment, '/merge-when-green');
assert.equal((await policy(ready, { currentVersion: 'v0.4.0', updateType: 'minor' })).dependencyDashboardApproval, true);
assert.equal((await policy(mergeChildConfig(ready, { packageRules: [{ matchPackageNames: ['example'], automerge: false }] }))).automerge, false);
assert.equal((await policy(ready, { updateType: 'major', depName: 'some-runtime', packageName: 'some-runtime' })).automerge, false);
