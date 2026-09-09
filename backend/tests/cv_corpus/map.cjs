// Execute production mapper and defaults, with Node's TypeScript stripper.
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const {stripTypeScriptTypes} = require('node:module');
const root = process.env.CV_SOURCE_ROOT || path.resolve(__dirname, '../../..');
const w = fs.readFileSync(path.join(root,'landing/src/app/components/OnboardingWizard.tsx'),'utf8');
const s = fs.readFileSync(path.join(root,'landing/src/app/components/onboarding/steps.tsx'),'utf8');
const start = s.indexOf('export function emptyCredentials(');
const empty = s.slice(start,s.indexOf('\n/* Placeholder',start)).replace('export function','function');
const apply = w.slice(w.indexOf('function applyCvParse('),w.indexOf('export default function OnboardingWizard'));
const rows=s.slice(s.indexOf('let rowSeq ='),s.indexOf('export type BoardCert')).replaceAll('export function','function');
const ctx = {}; vm.createContext(ctx); vm.runInContext(stripTypeScriptTypes(rows+'\n'+apply+'\n'+empty),ctx);
const input = JSON.parse(fs.readFileSync(0,'utf8'));
const result = input.map(p => { const current = ctx.emptyCredentials(); return {...current,...ctx.applyCvParse(p,current).patch}; });
process.stdout.write(JSON.stringify(result,(key,value)=>key === "rowId" ? undefined : value));
