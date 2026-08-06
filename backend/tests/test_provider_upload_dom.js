/* Verify the browser-side streaming SHA-256 in frontend/provider/provider.js
   against Node's crypto, which is the reference implementation.

   This is not a formality. The client DECLARES the whole-file digest before a
   single byte is uploaded, and the server refuses the assembled bundle if it
   disagrees — so a wrong hasher does not degrade anything, it makes every large
   upload fail. The length-padding word in particular is only wrong ABOVE 512 MB
   (JS `>>> 32` is a no-op, so the naive shift silently truncates), which is
   exactly the size range this code exists to serve and exactly the range a
   small-input test would never reach.

   Run: node backend/tests/test_provider_upload_dom.js
*/
'use strict';

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const SRC = fs.readFileSync(
  path.join(__dirname, '..', '..', 'frontend', 'provider', 'provider.js'), 'utf8');

// Extract just the hasher from the shipped IIFE — no DOM, no fetch, no stubs.
function extract(name, kind) {
  const needle = kind === 'const' ? `const ${name} =` : `function ${name}(`;
  const start = SRC.indexOf(needle);
  if (start < 0) throw new Error(`could not find ${name} in provider.js`);
  if (kind === 'const') {
    const end = SRC.indexOf(');', start) + 2;
    return SRC.slice(start, end);
  }
  const brace = SRC.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < SRC.length; i++) {
    if (SRC[i] === '{') depth++;
    else if (SRC[i] === '}') { depth--; if (depth === 0) return SRC.slice(start, i + 1); }
  }
  throw new Error(`unbalanced braces extracting ${name}`);
}

function extractProto(name) {
  const start = SRC.indexOf(`Sha256.prototype.${name} = function`);
  if (start < 0) throw new Error(`could not find Sha256.prototype.${name}`);
  const brace = SRC.indexOf('{', start);
  let depth = 0;
  for (let i = brace; i < SRC.length; i++) {
    if (SRC[i] === '{') depth++;
    else if (SRC[i] === '}') { depth--; if (depth === 0) return SRC.slice(start, i + 2); }
  }
  throw new Error(`unbalanced braces extracting prototype.${name}`);
}

const src = [
  extract('K256', 'const'),
  extract('Sha256', 'function'),
  extractProto('_block'),
  extractProto('update'),
  extractProto('hex'),
  'module.exports = Sha256;',
].join('\n');

const Sha256 = (new Function('module', src + '\nreturn module.exports;'))({ exports: {} });

function ours(buf, sliceSize) {
  const h = new Sha256();
  for (let i = 0; i < buf.length; i += sliceSize) {
    h.update(new Uint8Array(buf.subarray(i, Math.min(i + sliceSize, buf.length))));
  }
  return h.hex();
}

function theirs(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

let failures = 0;
function check(label, got, want) {
  if (got === want) { console.log(`  ok   ${label}`); return; }
  failures++;
  console.log(`  FAIL ${label}\n       got  ${got}\n       want ${want}`);
}

console.log('streaming SHA-256 vs node crypto:');

// Canonical vectors first — if these are wrong, nothing else matters.
check('empty string', ours(Buffer.alloc(0), 64),
  'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855');
check('"abc"', ours(Buffer.from('abc'), 64),
  'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad');

// Sizes that straddle every boundary in the padding logic: exactly one block,
// one byte under the 56-byte length-field threshold, one byte over, and the
// two-block padding case.
for (const n of [1, 55, 56, 57, 63, 64, 65, 119, 120, 127, 128, 1000, 65536]) {
  const buf = crypto.randomBytes(n);
  check(`${n} bytes`, ours(buf, 64), theirs(buf));
}

// Slice boundaries that do NOT align to the 64-byte block, which is the real
// shape of reading a file in arbitrary chunks.
{
  const buf = crypto.randomBytes(300000);
  for (const slice of [1, 7, 63, 64, 65, 1000, 4096, 100003]) {
    check(`300000 bytes in ${slice}-byte slices`, ours(buf, slice), theirs(buf));
  }
}

// THE ONE THAT MATTERS: a length whose bit-count exceeds 32 bits. Below 512 MB
// a naive `bitLen >>> 32` looks perfect; at and above it, every digest is wrong.
// Verified by driving the hasher's total past the boundary with a repeated block
// rather than allocating the bytes.
{
  const block = Buffer.alloc(1024 * 1024, 0xab);
  const h = new Sha256();
  const ref = crypto.createHash('sha256');
  for (let i = 0; i < 600; i++) {           // 600 MB — past the 2^32-bit boundary
    h.update(new Uint8Array(block));
    ref.update(block);
  }
  check('600 MB (bit length > 2^32)', h.hex(), ref.digest('hex'));
}

if (failures) {
  console.error(`\n${failures} check(s) failed`);
  process.exit(1);
}
console.log('\nall checks passed');
