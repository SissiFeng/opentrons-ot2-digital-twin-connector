'use strict';
const $=id=>document.getElementById(id), token=document.querySelector('meta[name="bridge-token"]').content;
let reviewed=null, active=false, checking=false, activeProfile=null, shownEvents=0, generation=0;
const profiles={};
let site={managed:false,devices:[]}, observedRun=null, frameURL=null, frameKey=null;
const configs={
  ot2:{name:'OT-2',title:'OT-2 connection check',workflow:'Home & position readback',hint:'Left P10 GEN1 ×8 · Right P300 GEN2 ×8. Home and readback only.',caption:'Home on each backend, then read its state. Left p10_multi_v1.6; right p300_multi_v2.0 (operator-reported). Nominal visual model; mount alignment is not calibrated. Firmware mm and Matterix joint m stay separate.',steps:[['Home instrument','Home X / Y / Z / A / B / C; Matterix moves to its native home pose'],['Read position','Read firmware axes and connector homed flags; retain native Matterix observations']]},
  flex:{name:'Flex',title:'Flex tip cycle',workflow:'Tip pickup & release',hint:'PR #59 · left single-channel pipette. One execution, no automatic repeat.',caption:'Logical route only. Real motion uses your calibrated deck points; Matterix uses its native scene.',steps:[['Pick up tip','Approach rack A1 → attach → lift'],['Release tip','Move over waste → release → retract'],['Return to park','Move the empty pipette to its reviewed park point']]}
};
async function api(path,data){const r=await fetch('/api/'+path,{method:data===undefined?'GET':'POST',headers:{'X-Bridge-Token':token,'Content-Type':'application/json'},body:data===undefined?undefined:JSON.stringify(data)});const value=await r.json();if(!r.ok)throw Error(value.error||r.statusText);return value;}
function message(text){$('message').textContent=text;}
function connectorMessage(text,state){const node=$('connector-status');node.textContent=text;node.dataset.state=state;node.hidden=false;message(text);}
function profile(){const p=JSON.parse($('profile').value);if(p.schema!==instrument()+'.profile/1')throw Error('The profile belongs to a different instrument. Select that instrument or import its profile file first.');return p;}
function instrument(){return $('instrument').value;}
function invalidate(){$('ubuntu-launch').hidden=true;$('connector-status').hidden=true;generation++;reviewed=null;$('ready').checked=false;$('export').disabled=true;$('run').disabled=true;$('review-state').textContent='Configuration changed. Review a new plan.';}
function download(data,name,type){const url=URL.createObjectURL(new Blob([data],{type}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),5000);}
async function guarded(fn){try{await fn();}catch(e){message(e.message);}}
function updateForm(){const r=profile().real;$('connector-host').value=r.host;$('connector-host').readOnly=instrument()!=='ot2';$('connector-port').value=r.port;$('server-uuid').value=r.server_uuid;document.querySelectorAll('[data-binding]').forEach(input=>{input.value=r[input.dataset.binding]??'';});$('travel-z').value=r.pickup_clearance?.z??'';for(const name of ['pickup','drop','park'])for(const axis of ['x','y','z']){const input=$(name+'-'+axis);if(input)input.value=r[name]?.[axis]??'';}}
function applyForm(){try{const p=profile(),r=p.real;r.port=Number($('connector-port').value);if(instrument()==='flex'){document.querySelectorAll('[data-binding]').forEach(input=>{r[input.dataset.binding]=input.type==='number'?(input.value===''?null:Number(input.value)):input.value;});const height=$('travel-z').value===''?null:Number($('travel-z').value);for(const name of ['pickup','drop','park']){const point={};for(const axis of ['x','y','z']){const input=$(name+'-'+axis);point[axis]=input?(input.value===''?null:Number(input.value)):height;}r[name]=Object.values(point).some(v=>v===null)?null:point;}for(const name of ['pickup','drop'])r[name+'_clearance']=r[name]&&height!==null?{...r[name],z:height}:null;}$('profile').value=JSON.stringify(p,null,2);invalidate();}catch(e){message('Correct the profile before editing its binding: '+e.message);}}
document.querySelectorAll('.binding-fields input').forEach(input=>input.oninput=applyForm);
$('connector-host').oninput=()=>{try{const p=profile();p.real.host=$('connector-host').value.trim();p.real.server_uuid='';$('profile').value=JSON.stringify(p,null,2);$('server-uuid').value='';invalidate();}catch(e){message(e.message);}};
$('connector-port').oninput=()=>{try{const p=profile();p.real.port=Number($('connector-port').value);p.real.server_uuid='';$('profile').value=JSON.stringify(p,null,2);$('server-uuid').value='';invalidate();}catch(e){message(e.message);}};
function selectStep(index){document.querySelectorAll('.step').forEach((node,i)=>node.classList.toggle('selected',i===index));const ot2=instrument()==='ot2',positions=ot2?[[445,105],[445,105]]:[[92,246],[466,82],[300,345]];const point=positions[index];if(point){const tool=$(ot2?'ot2-tool':'flex-tool');tool.setAttribute('cx',point[0]);tool.setAttribute('cy',point[1]);}}
function modeChanged(){const mode=$('mode').value;if(site.managed){$('mode-help').textContent=mode==='sim-only'?'Starts native Matterix on Ubuntu and shows its actual rendered scene below.':mode==='shadow'?'Starts Matterix, waits for the scene, then runs both systems at paired step boundaries.':'Runs the configured physical device through its outbound gateway.';$('ready-label').textContent=mode==='sim-only'?'I reviewed this simulation plan.':(instrument()==='ot2'?'I cleared the OT-2 deck and removed attached tips/tools; I authorize one Home and readback run.':'I checked the calibrated physical setup and authorize one tip cycle.');$('run').textContent=mode==='sim-only'?'Simulate in Matterix':mode==='shadow'?'Run both':'Run device';$('ready').checked=false;$('run').disabled=true;return;}$('mode-help').textContent=mode==='shadow'?'Both peers complete each step before the next starts. Their observations and elapsed times remain separate.':mode==='real-only'?'Commands the physical instrument through its SiLA connector. Matterix is not started.':'Runs Matterix only. Export the Ubuntu script or connect its waiting service.';$('ready-label').textContent=(mode!=='real-only'?'The exact exported plan is loaded in Matterix and its service reports ready. ':'')+(mode!=='sim-only'?(instrument()==='ot2'?'I cleared the OT-2 deck and removed attached tips/tools; I authorize homing and readback.':'I checked the physical deck, calibrated points and installed pipette; I authorize this one hardware run.'):'');$('ready').checked=false;$('run').disabled=true;}
function setActive(value){active=value;document.querySelectorAll('.binding-fields input, .connection-fields input').forEach(input=>{input.disabled=value;});if(!value)activeProfile=null;for(const id of ['review','inspect','home','profile','import','instrument','backend','mode','device'])$(id).disabled=value;$('inspect').disabled=value||checking;$('run').disabled=value||!reviewed||!$('ready').checked;}
function renderInstrument(){const name=instrument(),c=configs[name],ot2=name==='ot2';$('workflow').options[0].textContent=c.workflow;$('workflow-hint').textContent=c.hint;$('deck-title').textContent=c.title;$('backend').options[0].textContent=site.managed?'Device gateway · real':'SiLA connector · real';$('binding-help').textContent=ot2?'Enter the Mac Tailscale IP and forwarded SiLA port, then check the physical connector identity. Use 127.0.0.1 only when the forward runs on this backend host.':'Import a qualified profile or enter your calibrated physical setup. Blank coordinates never authorize hardware.';$('flex-binding').hidden=ot2;$('home').hidden=ot2;$('flex-overlays').style.display=ot2?'none':'';$('ot2-overlays').style.display=ot2?'':'none';$('preview-caption').textContent=c.caption;$('stop-help').textContent=ot2?'OT-2 Stop can queue behind an active Home. It prevents later steps but is not an immediate physical emergency stop.':'A stopped browser or disconnected client does not confirm a physical stop.';$('deck').setAttribute('aria-label',`Schematic ${c.name} workflow: ${c.workflow}`);const list=document.querySelector('.steps');list.replaceChildren();c.steps.forEach(([title,description],index)=>{const li=document.createElement('li'),button=document.createElement('button'),number=document.createElement('span'),body=document.createElement('span'),strong=document.createElement('strong'),small=document.createElement('small');button.className='step'+(index===0?' selected':'');number.className='step-no';number.textContent=String(index+1).padStart(2,'0');strong.textContent=title;small.textContent=description;body.append(strong,small);button.append(number,body);button.onclick=()=>selectStep(index);li.append(button);list.append(li);});$('slots').replaceChildren();for(let row=0;row<4;row++)for(let col=0;col<3;col++){const x=65+col*157,y=38+row*84;$('slots').append(svgNode('rect',{x,y,width:146,height:74,rx:5,class:'slot'}));const t=svgNode('text',{x:x+7,y:y+15,class:'slot-label'});t.textContent=ot2?String((3-row)*3+col+1):'ABCD'[row]+(col+1);$('slots').append(t);}modeChanged();}
function renderOutcome(side,outcome){if(!outcome)return;$(side+'-state').textContent=outcome.status;const facts=outcome.observation?.facts||{},tip=facts['pipette.tip_attached'];if(instrument()==='flex'&&tip){$(side+'-evidence').textContent=`Tip ${tip.value?'present':'absent'} · ${tip.evidence} · ${outcome.observation.source}`;return;}const keys=Object.keys(facts).filter(k=>k.startsWith(side==='real'?'axis.':'joint.'));$(side+'-evidence').textContent=keys.length?keys.map(k=>`${k.split('.')[1]} ${Number(facts[k].value).toFixed(side==='real'?2:3)} ${facts[k].unit}`).join(' · ')+` · ${outcome.observation.source}`:(outcome.error||'Observation unavailable');}
async function poll(){if(!active)return;try{const job=await api('job');$('run-status').textContent=job.status.toUpperCase();for(const event of job.events.slice(shownEvents)){if(event.type==='simulation-starting'||event.type==='simulation-ready'){message(event.message);$('sim-state').textContent=event.type==='simulation-starting'?'Building scene…':'Ready';}if(event.type==='step-started'){selectStep(Number(event.operation_id.slice(0,2))-1);message(`Executing ${event.action.replaceAll('_',' ')}…`);}if(event.type==='backend-finished')renderOutcome(event.side,event.outcome);if(event.type==='step-finished'){renderOutcome('real',event.step.real?.outcome);renderOutcome('sim',event.step.sim?.outcome);}$('events').textContent+=JSON.stringify(event)+'\n';}shownEvents=job.events.length;if(job.status!=='running'){setActive(false);reviewed=null;$('ready').checked=false;$('run').disabled=true;$('review-state').textContent='Run '+job.status+'. Review a new plan before another run.';for(const side of ['real','sim'])if($(side+'-state').textContent==='Connecting')$(side+'-state').textContent=job.status==='held'?'Unavailable':'No result';message(job.report?.error||`Run ${job.status}. Report: ${job.output}`);return;}}catch(e){message(e.message+' — connection loss does not confirm physical stop.');}setTimeout(poll,500);}
$('review').onclick=()=>guarded(async()=>{const submitted=profile(),version=generation;const value=await api('review',{profile:submitted});if(version!==generation||JSON.stringify(profile())!==JSON.stringify(submitted)){message('Configuration changed during review. Review again.');return;}reviewed=value;$('review-state').textContent=`Run ${value.plan.binding.run_id.slice(0,8)} · profile ${value.plan.binding.profile_sha256.slice(0,12)}`;$('export').disabled=false;$('ready').checked=false;$('run').disabled=true;if(site.managed){message('Plan reviewed. Choose Simulate, Run device or Run both, acknowledge the plan, then start.');}else{message('Plan reviewed. The exact bundle is saved on Ubuntu. After activating Isaac and setting MATTERIX_ROOT / ASSETS_ROOT, run: '+value.serve_command);$('ubuntu-command').textContent=value.serve_command;$('ubuntu-launch').hidden=false;}});
$('export').onclick=()=>guarded(async()=>{const r=await fetch('/api/bundle',{headers:{'X-Bridge-Token':token}});if(!r.ok)throw Error((await r.json()).error);download(await r.arrayBuffer(),instrument()+'-test.zip','application/zip');message('Bundle downloaded. Extract it on Ubuntu and follow README.txt.');});
$('run').onclick=()=>guarded(async()=>{if(!reviewed||!$('ready').checked)throw Error('Review and acknowledge the run first.');if(JSON.stringify(profile())!==JSON.stringify(reviewed.plan.profile)){invalidate();throw Error('Visible profile differs from the reviewed plan');}activeProfile=reviewed.plan.profile;setActive(true);try{const started=await api('run',{fingerprint:reviewed.fingerprint,mode:$('mode').value,hardware:true});observedRun=started.run_id;}catch(error){setActive(false);throw error;}shownEvents=0;$('events').textContent='';$('export').disabled=true;$('real-state').textContent=$('mode').value==='sim-only'?'Not selected':'Connecting';$('sim-state').textContent=$('mode').value==='real-only'?'Not selected':'Connecting';poll();});
$('inspect').onclick=async()=>{
  if(checking||active)return;
  checking=true;
  $('inspect').disabled=true;
  $('inspect').textContent='Checking connector…';
  $('inspect').setAttribute('aria-busy','true');
  try{
    const submitted=profile(),version=generation;
    connectorMessage(site.managed?'Checking physical identity through the device gateway… No robot motion is requested.':`Checking ${submitted.real.host}:${submitted.real.port} from the Bridge backend… No robot motion is requested.`,'pending');
    const value=await api('inspect',{profile:submitted});
    if(version!==generation||JSON.stringify(profile())!==JSON.stringify(submitted))throw Error('Configuration changed during the check. Check the connector again.');
    if(value.is_simulating!==false)throw Error('Connect the physical instrument service. The connected connector reports simulation mode.');
    const p=profile();if(site.managed&&p.real.server_uuid!==value.server_uuid)throw Error('Physical identity differs from the configured device');p.real.server_uuid=value.server_uuid;
    if(instrument()==='flex'){p.real.pipette_id=value.pipette.id;p.real.pipette_model=value.pipette.model;}
    $('profile').value=JSON.stringify(p,null,2);updateForm();invalidate();
    $('real-state').textContent=`Physical ${configs[instrument()].name} connected`;
    $('real-evidence').textContent=instrument()==='ot2'?'Firmware position and homing status received':`Tip ${value.tip_present?'present':'absent'} · ${value.pipette.model}`;
    $('events').textContent=JSON.stringify(value,null,2);
    connectorMessage(`Physical ${configs[instrument()].name} connected. Identity and state received. Review the plan before running.`,'connected');
  }catch(e){
    $('real-state').textContent='Check failed';
    $('real-evidence').textContent='No current connection confirmation';
    connectorMessage('Connection check failed: '+e.message,'error');
  }finally{
    checking=false;
    $('inspect').disabled=active;
    $('inspect').textContent='Check connector';
    $('inspect').setAttribute('aria-busy','false');
  }
};
$('home').onclick=()=>guarded(async()=>{if(!confirm('Home the physical instrument now? Check that its deck is clear and no tip is attached.'))return;activeProfile=profile();setActive(true);try{const result=await api('home',{profile:activeProfile,hardware:true});message('Homing command completed.');$('events').textContent=JSON.stringify(result,null,2);}finally{setActive(false);}});
$('stop').onclick=()=>guarded(async()=>{const result=await api('stop',{profile:activeProfile||profile()});message('Connector returned the stop response below. Inspect the machine status and physical instrument.');$('events').textContent+='\n'+JSON.stringify(result,null,2);});
$('save-profile').onclick=()=>guarded(async()=>download(JSON.stringify(profile(),null,2),instrument()+'-profile.json','application/json'));
$('import').onchange=()=>guarded(async()=>{const f=$('import').files[0];if(!f)return;if(f.size>100000)throw Error('Profile is too large');const value=JSON.parse(await f.text()),p=value.profile||value;const name=p.schema==='ot2.profile/1'?'ot2':p.schema==='flex.profile/1'?'flex':null;if(!name)throw Error('Select a supported OT-2 or Flex console profile');$('instrument').value=name;$('profile').value=JSON.stringify(p,null,2);renderInstrument();updateForm();invalidate();});
$('profile').oninput=()=>{invalidate();try{updateForm();}catch(e){message(e.message);}};$('mode').onchange=modeChanged;$('ready').onchange=()=>{$('run').disabled=!reviewed||!$('ready').checked||active;};
$('instrument').onchange=()=>guarded(async()=>{if(site.managed){selectDeviceList();return;}const target=instrument();try{const p=JSON.parse($('profile').value);if(['ot2.profile/1','flex.profile/1'].includes(p.schema))profiles[p.schema==='ot2.profile/1'?'ot2':'flex']=p;}catch{}invalidate();const version=generation;const p=profiles[target]||await api('profile/'+target);if(generation!==version||instrument()!==target)return;$('profile').value=JSON.stringify(p,null,2);renderInstrument();updateForm();$('real-state').textContent='Not connected';$('real-evidence').textContent='No hardware observations received';$('sim-state').textContent='Not started';$('sim-evidence').textContent='No simulated observations received';$('events').textContent='';$('run-status').textContent='IDLE';message(`${configs[target].name} selected. Check its physical connector or review a Matterix-only plan.`);});
function svgNode(name,attrs){const n=document.createElementNS('http://www.w3.org/2000/svg',name);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);return n;}
for(let row=0;row<8;row++)for(let col=0;col<8;col++)$('tips').append(svgNode('circle',{cx:90+col*12,cy:217+row*12,r:3.4,class:'tip'}));

function clearDisplayedRun(){
  $('real-state').textContent='Not connected';$('real-evidence').textContent='No hardware observations received';
  $('sim-state').textContent='Not started';$('sim-evidence').textContent='No simulated observations received';
  $('events').textContent='';$('run-status').textContent='IDLE';
  message('Select a configured device and review its plan.');
}
function selectDeviceList(){
  clearDisplayedRun();
  const items=site.devices.filter(d=>d.profile.schema===instrument()+'.profile/1');
  $('device').replaceChildren();
  for(const d of items){const option=document.createElement('option');option.value=d.id;option.textContent=d.name;$('device').append(option);}
  if(!items.length){$('profile').value=JSON.stringify(profiles[instrument()],null,2);invalidate();renderInstrument();updateForm();$('review').disabled=true;$('inspect').disabled=true;$('device-status').textContent='No device configured for this instrument. Ask the site operator to add it.';return;}
  selectDevice();
}
function selectDevice(){
  clearDisplayedRun();
  const device=site.devices.find(d=>d.id===$('device').value);if(!device)return;
  $('profile').value=JSON.stringify(device.profile,null,2);invalidate();renderInstrument();updateForm();
  $('review').disabled=false;$('inspect').disabled=!device.online;
  $('device-status').textContent=device.fault|| (device.online?'Gateway online · ready for a read-only check':'Gateway offline · simulation is available; start the device-side gateway for hardware');
}
$('device').onchange=selectDevice;
async function updateNative(){
  if(!site.managed)return;
  const value=await api('native'),frame=value.viewer;
  $('native-status').textContent=value.status.toUpperCase();$('native-log').textContent=value.log_tail||'';
  if(!frame||frame.status!=='available'){
    $('native-frame').hidden=true;$('native-placeholder').hidden=false;
    $('native-placeholder').textContent=value.error||frame?.error||(value.status==='starting'?'Matterix is building the native scene…':'No native frame available for this run.');
    $('native-caption').textContent='Native view unavailable. The planned diagram above is only a schematic.';return;
  }
  const key=frame.binding.run_id+':'+frame.sequence;
  if(key!==frameKey){
    const response=await fetch('/api/native/frame?run_id='+encodeURIComponent(frame.binding.run_id),{signal:AbortSignal.timeout(4000),headers:{'X-Bridge-Token':token}});
    if(!response.ok)throw Error('Native frame unavailable');
    const url=URL.createObjectURL(await response.blob());$('native-frame').src=url;
    if(frameURL)URL.revokeObjectURL(frameURL);frameURL=url;frameKey=key;
  }
  $('native-frame').hidden=false;$('native-placeholder').hidden=true;
  const fresh=value.status==='ready'&&Date.now()-Date.parse(frame.captured_at)<3000;
  $('native-caption').textContent=`${fresh?'Live native view':'Last native frame · not live'} · captured ${new Date(frame.captured_at).toLocaleTimeString()} · ${frame.binding.device_id} · run ${frame.binding.run_id.slice(0,8)} · simulated scene`;
}
async function refreshApplication(){
  try{
    if(!active){
      const job=await api('job');
      if(job.status==='running'&&job.run_id!==observedRun){
        observedRun=job.run_id;reviewed=null;shownEvents=0;activeProfile=job.profile;
        if(job.profile){$('instrument').value=job.profile.schema.startsWith('ot2')?'ot2':'flex';$('profile').value=JSON.stringify(job.profile,null,2);renderInstrument();updateForm();}
        $('mode').value=job.mode||'sim-only';modeChanged();setActive(true);$('events').textContent='';poll();
      }
    }
    if(site.managed){await updateNative();site=await api('site');const d=site.devices.find(d=>d.id===$('device').value);if(d){$('device-status').textContent=d.fault||(d.online?'Gateway online':'Gateway offline');if(!active&&!checking)$('inspect').disabled=!d.online;}}
  }catch(e){message(e.message+' — no physical stop is confirmed by a connection failure.');}
  setTimeout(refreshApplication,1000);
}
guarded(async()=>{
  site=await api('site');profiles.ot2=await api('profile/ot2');profiles.flex=await api('profile/flex');
  $('profile').value=JSON.stringify(profiles.ot2,null,2);
  for(const id of ['device-picker','native-panel'])$(id).hidden=!site.managed;
  if(site.managed){for(const id of ['binding-editor','export','manual-launch'])$(id).hidden=true;selectDeviceList();}
  else{renderInstrument();updateForm();}
  refreshApplication();
});
