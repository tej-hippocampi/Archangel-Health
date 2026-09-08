const fs=require('node:fs'),path=require('node:path');
const {JSDOM}=require('jsdom');
const dom=new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>',{url:'http://local.test'});
for(const key of ['window','document','HTMLElement','HTMLInputElement','HTMLTextAreaElement','Event','MouseEvent','KeyboardEvent','Node'])global[key]=dom.window[key];
Object.defineProperty(global,'navigator',{value:dom.window.navigator,configurable:true});
global.IS_REACT_ACT_ENVIRONMENT=true;
global.fetch=async()=>({ok:false}); // no outbound request permitted
const React=require('react');const {act}=React;
const variant=process.argv[2]||'current';
const {mount}=require('./generated/'+variant+'/form.cjs');
const results=[];
let ctrl;
async function setup(options={}){if(ctrl)await act(async()=>ctrl.unmount());document.getElementById('root').innerHTML='';await act(async()=>{ctrl=mount(document.getElementById('root'),options);});}
const phone=()=>document.querySelector('input[type="tel"]');
const inputSet=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;
const areaSet=Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set;
async function input(el,value){await act(async()=>{(el.tagName==='TEXTAREA'?areaSet:inputSet).call(el,value);el.dispatchEvent(new Event('input',{bubbles:true}));});}
async function focus(el){await act(async()=>el.focus());}
async function click(el){if(!el)throw Error('Missing click target');await act(async()=>el.click());}
const header=id=>document.querySelector('button[aria-controls="'+id+'"]');
const findLabel=text=>[...document.querySelectorAll('div,label,span')].find(e=>e.textContent.trim().toLowerCase()===text.toLowerCase()&&!e.querySelector('div,label,span'));
const toggle=(label,value)=>[...findLabel(label).parentElement.querySelectorAll('button')].find(x=>x.textContent===value);
function record(name,actual,expected){results.push({name,actual,expected,passed:JSON.stringify(actual)===JSON.stringify(expected)});}
(async()=>{
 await setup();let el=phone();await focus(el);await input(el,'2');record('phone_node_survives_one_character',el===phone(),true);record('phone_focus_survives_one_character',document.activeElement===phone(),true);
 await setup();el=phone();await focus(el);for(const ch of '2025550147'){let active=document.activeElement;if(active.tagName==='INPUT')await input(active,active.value+ch);}record('ten_digits_without_reclick',phone().value,'2025550147');
 await setup({reviewMode:false,phase:1});el=phone();await focus(el);await input(el,'2');record('nonreview_phone_identity',el===phone(),true);record('nonreview_phone_focus',document.activeElement===phone(),true);
 await setup();let yes=toggle('Have you finished residency?','Yes');let training=document.getElementById('onb-sec-training');await focus(yes);await click(yes);record('residency_yes_node_survives',yes===toggle('Have you finished residency?','Yes'),true);record('residency_yes_focus_survives',document.activeElement===toggle('Have you finished residency?','Yes'),true);record('training_section_survives_yes',training===document.getElementById('onb-sec-training'),true);record('residency_yes_value_updates',ctrl.data.credentials.residencyCompleted,true);
 await setup();await click(header('onb-sec-identity'));record('identity_manual_collapse',header('onb-sec-identity').getAttribute('aria-expanded'),'false');await click(toggle('Have you finished residency?','Yes'));record('unrelated_edit_preserves_manual_collapse',header('onb-sec-identity').getAttribute('aria-expanded'),'false');
 await setup();await click(header('onb-sec-focus'));let niche=document.querySelector('textarea');await focus(niche);await input(niche,'Dialysis');record('textarea_node_survives',niche===document.querySelector('textarea'),true);record('textarea_focus_survives',document.activeElement===niche,true);
 await setup({credentials:{boardCertifications:[{board:'ABIM',specialty:'Nephrology',subspecialty:'',active:false}]},chips:['boardCertifications']});let board=document.querySelector('input[placeholder="American Board of Internal Medicine"]');await focus(board);await input(board,'ABIMX');record('cv_edit_keeps_focus',document.activeElement===board,true);record('cv_edit_clears_chip',ctrl.data.cvAutofilled.includes('boardCertifications'),false);
 await setup();let y=document.querySelector('input[placeholder="2010"]');await focus(y);await input(y,'2');record('residency_year_node_survives',y===document.querySelector('input[placeholder="2010"]'),true);record('residency_year_focus_survives',document.activeElement===y,true);
 await setup();let select=document.querySelector('select');await focus(select);await act(async()=>{select.dispatchEvent(new Event('change',{bubbles:true}));});record('country_select_node_survives',select===document.querySelector('select'),true);
 await setup();const buttons=[...document.querySelectorAll('button')];record('non_submit_controls_have_button_type',buttons.filter(b=>!b.getAttribute('type')).length,0);
 await setup();record('mobile_accessible_label_associated',phone().labels.length>0,true);
 await setup();record('empty_board_not_counted_as_filled',require('./generated/'+variant+'/form.cjs')?header('onb-sec-training').textContent.includes('0 of'):false,true);

 await setup();await click(header('onb-sec-focus'));let lang=document.querySelector('input[placeholder="List all languages"]');await focus(lang);await input(lang,'Spa');await click(toggle('Have you finished residency?','Yes'));record('unfinished_chip_draft_survives_other_edit',document.querySelector('input[placeholder="List all languages"]').value,'Spa');
 await setup();let textfield=document.querySelector('input[placeholder="Dr. Tej Patel"]');await focus(textfield);await input(textfield,'José Muñoz');record('unicode_name_value_survives',ctrl.data.credentials.fullLegalName,'José Muñoz');record('unicode_name_focus_survives',document.activeElement===textfield,true);
 await setup();let tel=phone();await focus(tel);await input(tel,'+1 (202) 555-0147');record('phone_paste_value',phone().value,'+1 (202) 555-0147');record('phone_paste_focus',document.activeElement===tel,true);
 await setup({credentials:{boardCertifications:[{board:'A',specialty:'Nephrology',subspecialty:'',active:false},{board:'B',specialty:'Nephrology',subspecialty:'',active:false},{board:'C',specialty:'Nephrology',subspecialty:'',active:false}]}});let third=document.querySelectorAll('input[placeholder="American Board of Internal Medicine"]')[2];let removes=[...document.querySelectorAll('button')].filter(x=>x.getAttribute('aria-label')==='Remove' || x.title==='Remove');await click(removes[1]);record('remove_middle_row_keeps_last_row_identity',third===document.querySelectorAll('input[placeholder="American Board of Internal Medicine"]')[1],true);record('remove_middle_row_keeps_last_row_value',ctrl.data.credentials.boardCertifications.map(x=>x.board),['A','C']);
 await setup();await click(header('onb-sec-focus'));let lang2=document.querySelector('input[placeholder="List all languages"]');await focus(lang2);await input(lang2,'Español');await click(header('onb-sec-focus'));await click(header('onb-sec-focus'));record('accordion_toggle_preserves_chip_draft',document.querySelector('input[placeholder="List all languages"]').value,'Español');
 const file=path.join(__dirname,variant+'-results.json');fs.writeFileSync(file,JSON.stringify({variant,scope:'Actual form rendered in JSDOM with React 18.3.1. No browser control, no network, no measured pixel scroll. Stable variant hoists only Group in a disposable copy.',checks:results},null,2));console.log(JSON.stringify({variant,total:results.length,pass:results.filter(x=>x.passed).length,failures:results.filter(x=>!x.passed)},null,2));await act(async()=>ctrl.unmount());
})().catch(e=>{console.error(e);process.exitCode=1;});
