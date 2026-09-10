const {test}=require('node:test'); const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {stripTypeScriptTypes}=require('node:module');
const root=path.resolve(__dirname,'..');
const w=fs.readFileSync(path.join(root,'src/app/components/OnboardingWizard.tsx'),'utf8');
const s=fs.readFileSync(path.join(root,'src/app/components/onboarding/steps.tsx'),'utf8');
const a=s.indexOf('export function emptyCredentials(');
const empty=s.slice(a,s.indexOf('\n/* Placeholder',a)).replace('export function','function');
const apply=w.slice(w.indexOf('function applyCvParse('),w.indexOf('export default function OnboardingWizard'));
const rows=s.slice(s.indexOf('let rowSeq ='),s.indexOf('export type BoardCert')).replaceAll('export function','function');
const ctx={};vm.createContext(ctx);vm.runInContext(stripTypeScriptTypes(rows+'\n'+apply+'\n'+empty),ctx);
const plain=v=>JSON.parse(JSON.stringify(v));
const parsed={ok:true,employer:'Example Hospital',mobile_phone:'+1 555 010 4444',practice_city:'Boston',clinical_focus:'CKD',licenses:[{state:'CA',number:'A123456',current:'yes'}],board_certifications_structured:[{board:'ABIM',specialty:'Nephrology',subspecialty:'',active:null}],training:[{kind:'fellowship',institution:'Example Clinic',specialty:'Nephrology',end_year:'2020'}]};
test('unknown board state stays unanswered including manual defaults',()=>{
 assert.equal(ctx.emptyCredentials().boardCertifications[0].active,null);
 assert.equal(ctx.applyCvParse(parsed,ctx.emptyCredentials()).patch.boardCertifications[0].active,null);
});
test('licence pair cannot combine a manual state and another number',()=>{
 const p=ctx.applyCvParse(parsed,{...ctx.emptyCredentials(),licenseState:'NY'}).patch;
 assert.equal(p.licenseNumber,undefined);assert.equal(p.licenseState,undefined);
});
test('untouched CV values refresh; manual corrections and cleared fields survive',()=>{
 const first=ctx.applyCvParse(parsed,ctx.emptyCredentials());
 const current={...ctx.emptyCredentials(),...first.patch,phone:'Doctor edit'};
 const p=ctx.applyCvParse({...parsed,employer:'New Hospital',mobile_phone:'Replacement'},current,parsed,first.filled).patch;
 assert.equal(p.healthSystem,'New Hospital');assert.equal(p.phone,undefined);
 const cleared={...current,healthSystem:'',cvManualFields:['healthSystem']};
 const q=ctx.applyCvParse({...parsed,employer:'New Hospital'},cleared,parsed,first.filled.filter(k=>k!=='healthSystem')).patch;
 // Deliberately cleared empty values must also remain empty (tracked by UI).
 assert.equal(q.phone,undefined);assert.equal(q.healthSystem,undefined);
});
test('replacement removes old suggestions absent from the new CV',()=>{
 const first=ctx.applyCvParse(parsed,ctx.emptyCredentials());
 const p=ctx.applyCvParse({ok:true},{...ctx.emptyCredentials(),...first.patch},parsed,first.filled).patch;
 assert.equal(p.healthSystem,'');assert.equal(p.licenseNumber,'');
});
test('no truncation of certification rows or fellowship subjects',()=>{
 const p=ctx.applyCvParse({...parsed,board_certifications_structured:Array.from({length:7},(_,i)=>({board:'B'+i,specialty:'S'+i}))},ctx.emptyCredentials()).patch;
 assert.equal(p.boardCertifications.length,7);assert.equal(p.fellowship[0].specialty,'Nephrology');
});
test('explicit contact facts reach their visible fields',()=>{
 const p=ctx.applyCvParse(parsed,ctx.emptyCredentials()).patch;
 assert.equal(p.phone,parsed.mobile_phone);assert.equal(p.practiceCity,'Boston');assert.equal(p.specialtyNiche,'CKD');
});
test('failed extraction never erases user answers',()=>{
 assert.deepEqual(plain(ctx.applyCvParse({ok:false},{...ctx.emptyCredentials(),fullLegalName:'Jane Smith'})),{patch:{},filled:[]});
});
test('saved suggestions refresh after reload without transient chips',()=>{
 const first=ctx.applyCvParse(parsed,ctx.emptyCredentials());
 const saved=plain({...ctx.emptyCredentials(),...first.patch});
 const p=ctx.applyCvParse({...parsed,employer:'New after reload'},saved).patch;
 assert.equal(p.healthSystem,'New after reload');
 assert.equal(p.fellowship[0].specialty,'Nephrology');
});
test('a manually cleared value stays empty after save and reload',()=>{
 const first=ctx.applyCvParse(parsed,ctx.emptyCredentials());
 const saved=plain({...ctx.emptyCredentials(),...first.patch,healthSystem:'',cvManualFields:['healthSystem']});
 assert.equal(ctx.applyCvParse({...parsed,employer:'New after reload'},saved).patch.healthSystem,undefined);
});
test('future residency is not recorded as completed',()=>{
 const p=ctx.applyCvParse({ok:true,training:[{kind:'residency',institution:'Example Hospital',end_year:'2040'}]},ctx.emptyCredentials()).patch;
 assert.equal(p.residencyCompletionYear,undefined);
});
test('additional licences retained including primary CV licence when manual pair conflicts',()=>{
 const p=ctx.applyCvParse({...parsed,licenses:[...parsed.licenses,{state:'MA',number:'B123456',current:''}]},ctx.emptyCredentials()).patch;
 assert.equal(p.additionalLicenses[0].state,'MA');
 const q=ctx.applyCvParse(parsed,{...ctx.emptyCredentials(),licenseState:'NY',licenseNumber:'USER123'}).patch;
 assert.equal(q.additionalLicenses[0].number,'A123456');
});
