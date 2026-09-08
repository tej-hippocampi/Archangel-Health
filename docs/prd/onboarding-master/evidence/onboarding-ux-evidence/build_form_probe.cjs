const fs=require('node:fs'),path=require('node:path');
const deps=process.env.ARCHANGEL_UX_DEPS || '/tmp/archangel-onboarding-ux-audit/node_modules';
const esbuild=require(path.join(deps,'esbuild'));
const repo=path.resolve(process.env.ARCHANGEL_REPO || process.cwd()),out=path.join(__dirname,'generated');fs.mkdirSync(out,{recursive:true});
let original=fs.readFileSync(path.join(repo,'landing/src/app/components/onboarding/steps.tsx'),'utf8');
const gs=original.indexOf('  const Group = (');const ge=original.indexOf('  const identityValid',gs);
if(gs<0||ge<gs)throw Error('Group definition not found');
let block=original.slice(gs,ge).trim().replace('const Group =','const StableGroup =').replace('{ n, children }','{ n, children, reviewMode, sections, openBy }').replace(': { n: 0 | 1 | 2; children: ReactNode }',': any');
let fixed=original.slice(0,gs)+original.slice(ge);
fixed=fixed.replace('export function Step5Credentials(',block+'\n\nexport function Step5Credentials(').replaceAll('<Group n=', '<StableGroup reviewMode={reviewMode} sections={sections} openBy={openBy} n=').replaceAll('</Group>','</StableGroup>');
for(const [variant,source] of [['current',original],['stable',fixed]]){
 const dir=path.join(out,variant);fs.mkdirSync(dir,{recursive:true});
 fs.writeFileSync(path.join(dir,'steps.tsx'),source);
 for(const f of ['primitives.tsx','completeness.ts'])fs.copyFileSync(path.join(repo,'landing/src/app/components/onboarding',f),path.join(dir,f));
 fs.writeFileSync(path.join(dir,'api.ts'),`export const API_BASE='/network-disabled';export const apiHeaders=()=>({});export const asclepiusPortalUrl=()=>'';export const redirectToAsclepiusPortal=()=>{throw Error('Navigation disabled')};`);
 fs.copyFileSync(path.join(repo,'landing/src/lib/npi.ts'),path.join(dir,'npi.ts'));
 const entry=`import React,{useState} from 'react';import {createRoot} from 'react-dom/client';import {Step5Credentials,emptyCredentials} from './steps';export function mount(el,options={}){let controller={};function Host(){const [data,setData]=useState({credentials:{...emptyCredentials('Mike Blum'),primarySpecialty:'Nephrology',...options.credentials},cvParsed:{ok:true},cvAutofilled:options.chips||[],...options.data});controller.data=data;controller.update=setData;return <Step5Credentials data={data} setData={patch=>setData(prev=>({...prev,...patch}))} onNext={async()=>false} onBack={()=>{}} eyebrow='LOCAL TEST' reviewMode={options.reviewMode!==false} phase={options.phase}/>;}const root=createRoot(el);root.render(<Host/>);controller.unmount=()=>root.unmount();return controller;}`;
 fs.writeFileSync(path.join(dir,'entry.tsx'),entry);
 esbuild.buildSync({entryPoints:[path.join(dir,'entry.tsx')],bundle:true,platform:'node',format:'cjs',outfile:path.join(dir,'form.cjs'),jsx:'automatic',external:['react','react-dom','react-dom/client','react/jsx-runtime'],alias:{'@/lib/auth-api':path.join(dir,'api.ts'),'@/lib/npi':path.join(dir,'npi.ts')},logLevel:'silent'});
}
console.log('Built actual current form and a disposable module-scope Group variant. No source files changed.');
