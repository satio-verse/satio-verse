import {test} from 'node:test';
import assert from 'node:assert/strict';
import {register} from 'node:module';
const THREE_URL=new URL('../../web/vendor/three/build/three.module.js',import.meta.url).href;
const STATIC_URL=new URL('../../web/',import.meta.url).href;
const PET_URL=new URL('../../desktop/web/',import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n){
 if(s==='three')return {url:${JSON.stringify(THREE_URL)},shortCircuit:true};
 if(s.startsWith('/pet/'))return {url:new URL(s.slice(5),${JSON.stringify(PET_URL)}).href,shortCircuit:true};
 if(s.startsWith('/static/'))return {url:new URL(s.slice(8),${JSON.stringify(STATIC_URL)}).href,shortCircuit:true};return n(s,c);}
export async function load(u,c,n){const r=await n(u,c);return u.endsWith('/web/locomotion.js')?{...r,source:r.source+'\\nexport { Locomotion };'}:r;}`)}`);
globalThis.location={search:''};
const T=await import('three');
const {Locomotion}=await import('../../web/locomotion.js');
const {cupCenter,liveCupCenter,cupPickItem,cupPickError,cupPutError,planCupPick,clipGeometry}=await import('../../web/cup-pick.js');
const {grab,place}=await import('../../desktop/web/room-acts.js');
function rig(){
 const scene=new T.Group(),hips=new T.Bone(),up=new T.Bone(),lo=new T.Bone(),hand=new T.Bone(),mid=new T.Bone();
 scene.add(hips);hips.add(up);up.add(lo);lo.add(hand);hand.add(mid);
 hips.position.y=.8;up.position.set(-.1,.4,0);lo.position.set(0,0,.2);hand.position.set(0,0,.2);mid.position.set(0,0,.06);
 scene.updateMatrixWorld(true);
 const item={id:'cup',grip:'cup',size:new T.Vector3(.09,.07,.06),half:.035,held:null,on:'table',mesh:new T.Object3D()};
 const L=Object.create(Locomotion.prototype);Object.assign(L,{vrm:{scene,humanoid:{getNormalizedBoneNode:n=>n==='rightMiddleProximal'?mid:null}},
 nodes:{hips,rightUpperArm:up,rightLowerArm:lo,rightHand:hand},avatar:{collision:()=>[]},hipHeight:.8,items:{cup:item},stats:{},char:{x:0,z:0,yaw:0},_grabGap(){},_standBoxes:()=>[]});
 item.mesh.position.copy(liveCupCenter(L,item,'right'));return {L,item,scene,hand,mid};
}
function pending(f){
 const task={item:f.item,side:'right',target:f.item.mesh.position.clone(),ready:true,resolved:false,bound:false,wait:0,finished:false,finish(){this.finished=true;}};
 f.L._cupPickTask=task;f.L.action={grab:{hold:{cupTask:task}},fired:false};return task;
}
test('cup center uses the visible radius/height convention and inverse is exact for either normal branch',()=>{
 const w=new T.Vector3(1,2,3),m=new T.Vector3(1.06,2,3),size=new T.Vector3(.09,.07,.06);
 for(const n of [new T.Vector3(0,0,1),new T.Vector3(0,-1,0)]){
  const c=cupCenter(w,m,n,size),target=new T.Vector3(2,1,0),shift=target.clone().sub(c);
  assert.ok(cupCenter(w.clone().add(shift),m.clone().add(shift),n,size).distanceTo(target)<1e-12);
 }
 assert.equal(cupPickItem({grip:'cup',physical:true,size}),false);assert.equal(cupPickItem({grip:'book',size}),false);
});
test('final displayed center binds only after the gate and marks the proxy convention',()=>{
 const f=rig(),task=pending(f);assert.equal(f.item.held,null);assert.equal(f.L._cupPickFinalize(1/30),true);
 assert.equal(f.item.held,'right');assert.equal(f.item.cupBinding,true);assert.equal(task.bound,true);assert.equal(f.L.lastCupPickContact.kind,'binding-center');
 assert.equal(f.L._cupPickFinalize(1/30),false);assert.ok(f.L._palmHold(f.item,'right').distanceTo(task.target)<1e-12);
});
test('post-filter center miss times out without bind or success, even with zero raw IK error',()=>{
 const f=rig(),task=pending(f);f.hand.position.x+=.05;f.scene.updateMatrixWorld(true);
 for(let i=0;i<30;i++)f.L._cupPickFinalize(1/30);
 assert.equal(f.item.held,null);assert.equal(task.bound,false);assert.equal(task.error.code,'CUP_PICK_CONTACT');assert.equal(task.resolved,true);
 assert.equal(f.L.lastCupPickContact.passed,false);
});
test('cancelled pending bind cannot bind later; stale task cannot cancel a new task; held cup stays held',()=>{
 const f=rig(),a=pending(f);assert.equal(f.L._cancelCupPick('stop',a),true);assert.equal(f.L._cupPickFinalize(1/30),false);assert.equal(f.item.held,null);
 const b=pending(f);assert.equal(f.L._cancelCupPick('old',a),false);assert.equal(b.resolved,false);f.L._cupPickFinalize(1/30);
 assert.equal(f.L._cancelCupPick('stop',b),false);assert.equal(f.item.held,'right');
});
test('cup contact/navigation errors never reach the unverified crouch attachment or reward',async()=>{
 for(const code of ['CONTACT','UNREACHABLE','CANCELLED']){
  const f=rig(),err=cupPickError(code,'走不到，没有合适的站位'),forbidden=()=>{throw Error('fallback or reward');};
  const ctx={r:{layer:f.L,practice:{attempt:forbidden},poses:{set:forbidden},body:{begin:forbidden}},run:async()=>{throw err;},learn:forbidden};
  await assert.rejects(grab(ctx,'cup'),e=>e===err);assert.equal(f.item.held,null);assert.equal(f.item.origin,undefined);
 }
});
function plannerRig(){
 const f=rig(),L=f.L,bones=Object.keys(L.nodes),rest={hips:[0,.8,0],rightUpperArm:[-.1,1.2,0],rightLowerArm:[-.1,1.2,.2],rightHand:[-.1,1.2,.4],rightHandEnd:[-.1,1.2,.46]};
 Object.assign(L,{db:{bones},align:Object.fromEntries(bones.map(n=>[n,[0,0,0,1]])),parentOf:{hips:null,rightUpperArm:'hips',rightLowerArm:'rightUpperArm',rightHand:'rightLowerArm'},
 rest:Object.fromEntries(Object.entries(rest).map(([n,v])=>[n,new T.Vector3(...v)])),grid:{x0:-2,z0:-2,w:40,h:40,cell:.1,open:new Uint8Array(1600).fill(1)},_clipStrain:()=>0,
 acts:{fps:30,clips:[{start:0,end:3}],sample(fr,out){bones.forEach((_,i)=>out[i]=[0,0,0,1]);return{hips:[0,1,0],root:[0,0,0]};}}});
 f.item.mesh.position.set(.1,1.15,.8);return f;
}
test('planner derives a reachable legal grid point without live writes or consuming randomness',()=>{
 const f=plannerRig(),L=f.L;const before=JSON.stringify([L.char,...Object.values(L.nodes).map(n=>[n.position.toArray(),n.quaternion.toArray()])]);
 const random=Math.random;Math.random=()=>{throw Error('planner consumed RNG');};let plan;
 try {plan=planCupPick(L,f.item,'right',[{clip:0,eventOffset:1}]);}finally{Math.random=random;}
 assert.ok(plan);assert.ok(plan.reach<=plan.arm*.97);assert.equal(JSON.stringify([L.char,...Object.values(L.nodes).map(n=>[n.position.toArray(),n.quaternion.toArray()])]),before);
 assert.ok(L.lastCupPickPlan.considered>0);assert.ok(Math.abs(clipGeometry(L,{clip:0,eventOffset:1},'right').shoulder.y-1.2)<1e-12);
});

test('same-frame cancellation predicate prevents bind before the cancellation poll',()=>{
 const f=rig(),task=pending(f);task.isOperationActive=()=>false;
 assert.equal(f.L._cupPickFinalize(1/30),false);assert.equal(task.error.code,'CUP_PICK_CANCELLED');assert.equal(f.item.held,null);
});
test('cup clip stays at the screened root during the gate and after bind without source drift',()=>{
 const {L}=rig(),task={resolved:false};Object.assign(L,{acts:{fps:30,sample(f,out){return{hips:[0,1,0],root:[.2,.3,.1]};}},pose:[],inert:{pos:[0,0,0]},seatAmount:0,_headIn:0,handSpeed:1,
 action:{frame:1,start:0,end:10,event:1,fired:false,grab:{hold:{cupTask:task}},warp:{x:0,z:0,yaw:0},warpUntil:1,progress:1}});
 for(let i=0;i<20;i++)L._playAction(1/30,i/30);
 assert.deepEqual(L.char,{x:0,z:0,yaw:0});assert.equal(L.action.frame,1);
 task.resolved=true;L._playAction(1/30,1);assert.deepEqual(L.char,{x:0,z:0,yaw:0});assert.ok(L.action.frame>1);
 delete L.action.grab.hold.cupTask;L._playAction(1/30,2);assert.ok(L.char.z>0);assert.ok(L.char.yaw>0);
});
test('later clips are tried when the first event fits but its final body pose collides',()=>{
 const {L,item}=plannerRig();L.acts.clips=[{start:0,end:3},{start:3,end:6}];
 L.acts.sample=(f,out)=>{L.db.bones.forEach((_,i)=>out[i]=[0,0,0,1]);return{hips:[0,f===2?4:1,0],root:[0,0,0]};};
 L.avatar.collision=()=>[{min:[-4,3,-4],max:[4,4,4],parts:[{min:[-4,3,-4],max:[4,4,4]}]}];
 const first={clip:0,eventOffset:1},second={clip:1,eventOffset:1};
 assert.equal(planCupPick(L,item,'right',[first]),null);
 assert.equal(planCupPick(L,item,'right',[first,second]).g,second);
});
test('malformed dimensions, nonfinite arm length and zero grid cells fail closed',()=>{
 assert.equal(cupCenter(new T.Vector3(NaN,0,0),new T.Vector3(),new T.Vector3(0,0,1),new T.Vector3(1,1,1)),null);
 const {L,item}=plannerRig();L.grid.cell=0;assert.equal(planCupPick(L,item,'right',[{clip:0,eventOffset:1}]),null);
 L.grid.cell=.1;L.nodes.rightHand.position.x=Infinity;assert.equal(planCupPick(L,item,'right',[{clip:0,eventOffset:1}]),null);
});

function pendingPut(f,offset=[0,0,0]) {
 const t=pending(f);delete f.L._cupPickTask;Object.assign(t,{kind:'put',key:'_cupPutTask',released:false,on:'table'});
 t.target.add(new T.Vector3(...offset));f.item.held='right';f.item.cupBinding=true;f.L._cupPutTask=t;return t;
}
test('put releases at measured center, keeps orientation, and never snaps the accepted horizontal error',()=>{
 const f=rig(),task=pendingPut(f,[.005,0,0]),actual=liveCupCenter(f.L,f.item,'right');
 f.item.mesh.quaternion.setFromAxisAngle(new T.Vector3(0,1,0),.4);const q=f.item.mesh.quaternion.clone();
 assert.equal(f.L._cupPickFinalize(1/30),true);assert.equal(task.released,true);assert.equal(f.item.held,null);
 assert.ok(f.item.mesh.position.distanceTo(actual)<1e-12);assert.ok(f.item.mesh.position.distanceTo(task.target)>.0049);
 assert.ok(f.item.mesh.quaternion.angleTo(q)<1e-12);assert.equal(f.L.lastCupPutContact.kind,'support-center');
});
test('put support miss fails while keeping the cup even when total center error is below 10 mm',()=>{
 const f=rig(),task=pendingPut(f,[0,.004,0]);for(let i=0;i<30;i++)f.L._cupPickFinalize(1/30);
 assert.equal(task.released,false);assert.equal(f.item.held,'right');assert.equal(task.error.code,'CUP_PUT_CONTACT');
 assert.equal(f.L.lastCupPutContact.passed,false);assert.ok(f.L.lastCupPutContact.error<.01);
});
test('expired put cannot release later or revoke a different put; an active safe return can finish',()=>{
 const f=rig(),a=pendingPut(f);a.isOperationActive=()=>false;f.L._cupPickFinalize(1/30);
 assert.equal(a.error.code,'CUP_PUT_CANCELLED');assert.equal(f.item.held,'right');assert.equal(a.released,false);
 const b=pendingPut(f);b.isOperationActive=()=>true;assert.equal(f.L._cancelCupPut('old',a),false);
 assert.equal(f.L._cupPickFinalize(1/30),true);assert.equal(b.released,true);assert.equal(f.L._cancelCupPut('late',b),false);
});
test('typed cup put failure cannot reach crouch release or report successful placement',async()=>{
 const f=rig();f.item.held='right';f.item.cupBinding=true;f.L.targets={table:{place:[0,1,0]}};
 const err=cupPutError('CONTACT','走不到，没有合适的站位'),forbidden=()=>{throw Error('fallback or reward');};
 const ctx={r:{layer:f.L,practice:{attempt:forbidden},poses:{set:forbidden},body:{begin:forbidden}},run:async()=>{throw err;},learn:forbidden};
 await assert.rejects(place(ctx,'table'),e=>e===err);assert.equal(f.item.held,'right');
});
test('put planner targets the support center rather than the current held center and preserves raw destination',()=>{
 const f=plannerRig(),L=f.L,target=f.item.mesh.position.clone();f.item.mesh.position.set(100,100,100);
 assert.ok(planCupPick(L,f.item,'right',[{clip:0,eventOffset:1}],target));
 f.item.origin={pos:target,on:'table'};let args;L._cupTransfer=(...a)=>{args=a;};L._cupPut(f.item,'home',()=>{},()=>{});
 assert.equal(args[1],'put');assert.deepEqual(args[2].toArray(),target.toArray());assert.equal(args[6],'home');assert.equal(args[3],'table');
});
