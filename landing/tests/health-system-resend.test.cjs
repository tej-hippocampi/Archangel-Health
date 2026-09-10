const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { stripTypeScriptTypes } = require('node:module');

const source = fs.readFileSync(path.join(__dirname, '../src/lib/auth-api.ts'), 'utf8');
const detail = source.slice(source.indexOf('async function readDetail('), source.indexOf('export async function healthSystemSignup('));
const resend = source.slice(source.indexOf('export async function healthSystemResendCode('), source.indexOf('export async function healthSystemVerify('));
const code = stripTypeScriptTypes(detail + resend.replace('export async', 'async'));
function load(fetch) {
  const context = vm.createContext({ fetch, API_BASE: '', apiHeaders: h => h });
  vm.runInContext(code, context);
  return context.healthSystemResendCode;
}

test('health-system resend reports transport rejection instead of success', async () => {
  const resendCode = load(async () => { throw new Error('offline'); });
  await assert.rejects(resendCode('dana@example.org'), { message: 'offline' });
});

test('health-system resend reports the server error and rate limit', async () => {
  for (const status of [429, 503]) {
    const resendCode = load(async () => ({ ok: false, status, json: async () => ({ detail: 'Please try again later.' }) }));
    await assert.rejects(resendCode('dana@example.org'), { message: 'Please try again later.' });
  }
});

test('health-system resend succeeds only after a successful response', async () => {
  let request;
  const resendCode = load(async (url, options) => { request = { url, options }; return { ok: true }; });
  await resendCode('dana@example.org');
  assert.equal(request.url, '/api/asclepius/hs/signup/resend');
  assert.equal(request.options.credentials, 'include');
  assert.deepEqual(JSON.parse(request.options.body), { email: 'dana@example.org' });
});
