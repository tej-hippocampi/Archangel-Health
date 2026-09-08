// Run with Node 24: node --test tests/credential-countries.test.cjs
// Execute the actual config and hook, with only React state/effect and fetch
// replaced. No browser, external registry, or account is contacted.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');
const root = path.resolve(__dirname, '..');
const names = JSON.parse(fs.readFileSync(path.join(root, 'src/lib/countries.json')));
const source = fs.readFileSync(path.join(root, 'src/app/components/onboarding/steps.tsx'), 'utf8');
const start = source.indexOf('export type RegistryFieldSpec');
const end = source.indexOf('const PRACTICE_SETTING_SUGGESTIONS', start);
assert.ok(start >= 0 && end > start);
const code = stripTypeScriptTypes(source.slice(start, end).replaceAll('export type', 'type'));

async function load(fetch) {
  let state;
  let effect;
  const ctx = { countryNames: names, fetch, API_BASE: '', apiHeaders: () => ({}),
    useState: initial => { state = initial; return [state, next => { state = next; }]; },
    useEffect: fn => { effect = fn; },
  };
  vm.createContext(ctx);
  vm.runInContext(code + '\nuseCredentialConfig();', ctx);
  const initial = state;
  effect();
  await new Promise(resolve => setImmediate(resolve));
  return { initial, current: state };
}
function allCountries(cfg) {
  assert.equal(cfg.countries.length, 250);
  assert.deepEqual(Array.from(cfg.countries, c => c.country).sort(), Object.keys(names).sort());
  assert.equal(cfg.countries.find(c => c.country === 'BR').method, 'document');
  assert.equal(cfg.countries.find(c => c.country === 'US').id_label, 'NPI number');
}

test('loading and failed configuration keep every country available', async () => {
  const {initial, current} = await load(async () => { throw Error('offline'); });
  allCountries(initial);
  allCountries(current);
});
test('HTTP error and empty country response retain the catalogue', async () => {
  for (const response of [{ok: false}, {ok: true, json: async () => ({countries: []})}]) {
    allCountries((await load(async () => response)).current);
  }
});
test('older API enriches registry details without shrinking the country choices', async () => {
  const india = {country: 'IN', country_name: 'India', id_label: 'Medical council registration number',
    method: 'scrape', extra_fields: [{key: 'stateCouncil'}]};
  const {current} = await load(async () => ({ok: true, json: async () => ({
    countries: [india], default: {method: 'document'}, qualifications: ['MBBS'],
  })}));
  allCountries(current);
  assert.equal(current.countries.find(c => c.country === 'IN').id_label, india.id_label);
  assert.equal(current.countries.find(c => c.country === 'IN').extra_fields[0].key, 'stateCouncil');
});
