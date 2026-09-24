const {test}=require('node:test'); const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const {stripTypeScriptTypes}=require('node:module');
const root=path.resolve(__dirname,'..');
const w=fs.readFileSync(path.join(root,'src/app/components/OnboardingWizard.tsx'),'utf8');
const s=fs.readFileSync(path.join(root,'src/app/components/onboarding/steps.tsx'),'utf8');
const a=s.indexOf('export function emptyCredentials(');
const empty=s.slice(a,s.indexOf('\n/* Placeholder',a)).replaceAll('export function','function');
const apply=w.slice(w.indexOf('function applyCvParse('),w.indexOf('export default function OnboardingWizard'));
const rows=s.slice(s.indexOf('let rowSeq ='),s.indexOf('export type BoardCert')).replaceAll('export function','function');
const ctx={};vm.createContext(ctx);vm.runInContext(stripTypeScriptTypes(rows+'\n'+apply+'\n'+empty),ctx);
const plain=v=>JSON.parse(JSON.stringify(v));
const parsed={ok:true,employer:'Example Hospital',mobile_phone:'+1 555 010 4444',practice_city:'Boston',clinical_focus:'CKD',licenses:[{state:'CA',number:'A123456',current:'yes'}],board_certifications_structured:[{board:'ABIM',specialty:'Nephrology',subspecialty:'',active:null}],training:[{kind:'fellowship',institution:'Example Clinic',specialty:'Nephrology',end_year:'2020'}]};
test('ambiguous replacement clears an untouched old specialty, preserving manual choice',()=>{
 const first=ctx.applyCvParse({ok:true,specialty:'nephrology',specialty_display:'Nephrology',specialty_status:'resolved'},ctx.emptyCredentials());
 const current={...ctx.emptyCredentials(),...first.patch};
 const ambiguous={ok:true,specialty:null,specialty_display:'Nephrology',specialty_status:'ambiguous'};
 assert.equal(ctx.applyCvParse(ambiguous,current).patch.primarySpecialty,'');
 assert.equal(ctx.applyCvParse(ambiguous,{...current,primarySpecialty:'Dermatology',cvManualFields:['primarySpecialty']}).patch.primarySpecialty,undefined);
 assert.equal(ctx.applyCvParse(ambiguous,ctx.emptyCredentials()).patch.primarySpecialty,undefined);
});
test('legacy display cannot bypass a null or pediatric specialty decision',()=>{
 assert.equal(ctx.applyCvParse({ok:true,specialty:null,specialty_display:'Nephrology'},ctx.emptyCredentials()).patch.primarySpecialty,undefined);
 assert.equal(ctx.applyCvParse({ok:true,specialty:'pediatric nephrology',specialty_display:'Nephrology'},ctx.emptyCredentials()).patch.primarySpecialty,'pediatric nephrology');
});
test('a resolved CV fills the specialty and preserves a doctor correction',()=>{
 const cv={ok:true,specialty:'dermatology',specialty_display:'Dermatology',specialty_status:'resolved'};
 assert.equal(ctx.applyCvParse(cv,ctx.emptyCredentials()).patch.primarySpecialty,'Dermatology');
 assert.equal(ctx.applyCvParse(cv,{...ctx.emptyCredentials(),primarySpecialty:'Pathology',cvManualFields:['primarySpecialty']}).patch.primarySpecialty,undefined);
});
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
test('failed or timed-out replacement clears only unchanged CV suggestions',()=>{
 const first=ctx.applyCvParse({ok:true,specialty:'nephrology'},ctx.emptyCredentials());
 for (const failure of [null,{ok:false}]) {
  const current={...ctx.emptyCredentials(),...first.patch};
  assert.equal(ctx.applyCvParse(failure,current).patch.primarySpecialty,'');
  assert.equal(ctx.applyCvParse(failure,{...current,primarySpecialty:'Dermatology',cvManualFields:['primarySpecialty']}).patch.primarySpecialty,undefined);
 }
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

test('unanswered countries remain neutral and international degrees survive autofill',()=>{
 const blank=ctx.emptyCredentials();
 assert.equal(blank.countryOfPractice,'');assert.equal(blank.countryOfLicensure,'');assert.equal(blank.countryOfDegree,'');
 for(const degree of ['MBBS','MBChB','MBBCh','BMBS','BM BCh','MB BCh BAO','Staatsexamen']){
  const {patch}=ctx.applyCvParse({ok:true,degrees:[degree]},blank);
  assert.equal(patch.degree,degree);assert.equal(patch.qualification,degree);
  assert.equal(patch.countryOfLicensure,undefined,'a degree does not establish a licensing country');
 }
});
test('switching registry countries preserves identifiers without cross-country reuse',()=>{
 const gb={...ctx.emptyCredentials(),countryOfLicensure:'GB',registrationNumber:'GMC-1234567',registryExtras:{note:'retained'}};
 const india={...gb,...ctx.changeLicensure(gb,'IN')};
 assert.equal(india.registrationNumber,'');assert.deepEqual(plain(india.registryExtras),{});
 india.registrationNumber='NMC-987';india.registryExtras={stateCouncil:'Delhi'};
 const back={...india,...ctx.changeLicensure(india,'GB')};
 assert.equal(back.registrationNumber,'GMC-1234567');assert.equal(back.registryExtras.note,'retained');
 const reloaded=plain(back);
 const resumed={...reloaded,...ctx.changeLicensure(reloaded,'IN')};
 assert.equal(resumed.registrationNumber,'NMC-987');assert.equal(resumed.registryExtras.stateCouncil,'Delhi');
});
test('choosing the first country keeps a registration number entered while country was unanswered',()=>{
 const current={...ctx.emptyCredentials(),registrationNumber:'GMC-7654321'};
 const chosen={...current,...ctx.changeLicensure(current,'GB')};
 assert.equal(chosen.registrationNumber,'GMC-7654321');
 assert.equal({...chosen,...ctx.changeLicensure(chosen,'IN')}.registrationNumber,'');
});
test('an adopted registration stays with its country across Outside-US resets',()=>{
 let current={...ctx.emptyCredentials(),registrationNumber:'GMC-7654321',registryExtras:{note:'UK evidence'}};
 for(const country of ['GB','US','','IN']){
  current={...current,...ctx.changeLicensure(current,country)};
  assert.equal(current.registrationNumber,country==='GB'?'GMC-7654321':'');
 }
 current={...current,...ctx.changeLicensure(current,'GB')};
 assert.equal(current.registrationNumber,'GMC-7654321');
 assert.equal(current.registryExtras.note,'UK evidence');
});
