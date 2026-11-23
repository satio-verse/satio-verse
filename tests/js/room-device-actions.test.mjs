import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';
import { readFileSync } from 'node:fs';
const THREE_URL = new URL('../../web/vendor/three/build/three.module.js', import.meta.url).href;
const STATIC_URL = new URL('../../web/', import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n) {
  if(s==='three') return {url:${JSON.stringify(THREE_URL)},shortCircuit:true};
  if(s.startsWith('/static/')) return {url:new URL(s.slice(8),${JSON.stringify(STATIC_URL)}).href,shortCircuit:true};
  return n(s,c); }`)}`);
const THREE = await import('three');
const { prepareDeviceRoom, turnDeviceChair, slideDeviceChair, rotatedBounds, createDeviceScripts } = await import('../../desktop/web/room-device-actions.js');
const { POSES } = await import('../../desktop/web/room-poses.js');
const near = (a,b) => assert.ok(Math.abs(a-b)<1e-8, `${a} != ${b}`);

test('bounds use world Y rotation around a non-origin pivot; four quarter turns do not grow bounds', () => {
  const box={min:[2, .2, 4],max:[3,.8,6]}, pivot=[2,0,4];
  const b=rotatedBounds(box,pivot,Math.PI/2);
  b.min.forEach((x,i)=>near(x,[2,.2,3][i])); b.max.forEach((x,i)=>near(x,[4,.8,4][i]));
  const c=rotatedBounds(box,pivot,2*Math.PI);
  c.min.forEach((x,i)=>near(x,box.min[i])); c.max.forEach((x,i)=>near(x,box.max[i]));
  assert.throws(()=>rotatedBounds(box,pivot,NaN),/无效/);
});
function chairFixture({ rigid=false, parentYaw=.3 }={}) {
  const parent=new THREE.Group(), chair=new THREE.Group(); parent.rotation.y=parentYaw; parent.add(chair);
  chair.name='Asset_Chair'; chair.quaternion.setFromAxisAngle(new THREE.Vector3(0,1,0),-parentYaw);
  parent.updateMatrixWorld(true);
  const part={min:[-.2,0,-.3],max:[.2,1,.3]}, group={id:'desk_chair',...part,parts:[part]};
  const entry={id:'desk_chair',kind:'chair',name:'椅子',box:{center:[0,0],size:[.4,.6],height:1,base:0},seat:{pos:[0,0],yaw:Math.PI/2,height:.5},approach:[1.2,0]};
  const scene={sceneId:'casa-luce-v8',floor:[[-3,-3],[3,-3],[3,3],[-3,3]],objects:[entry],decor:[],collisionParts:[group]};
  const calls=[], avatar={_collision:['old']};
  const r={scene,layer:{targets:{desk_chair:entry},items:{},avatar,char:{x:1.2,z:0}},body:{},
    props:{layout:{},cache:{},node:id=>id==='Asset_Chair'?chair:null,save:()=>calls.push('save'),
      toLive:()=>calls.push('live'),toRoom:()=>calls.push('room'),tween:async(d,fn)=>{for(let i=0;i<=4;i++)fn(i/4);}}};
  if(rigid){const items=new Map([['desk_chair#0',{}],['floor',{}]]);r.rigid={ready:Promise.resolve(),layer:{},world:{items,
    remove:id=>{calls.push(['remove',id]);items.delete(id);},addFixedBox:(id,min,max)=>{calls.push(['add',id]);items.set(id,{min,max});}}};}
  return {r,chair,calls};
}
test('chair front, seat/nav/body/avatar and fixed colliders agree with transformed geometry, including rotated parent', async()=>{
  const {r,chair,calls}=chairFixture({rigid:true}); prepareDeviceRoom(r);
  near(new THREE.Vector3(0,0,1).applyQuaternion(chair.getWorldQuaternion(new THREE.Quaternion())).x,1);
  near(r.scene.collisionParts[0].max[0],.3); near(r.scene.collisionParts[0].max[2],.2);
  assert.equal(r.layer.avatar._collision,null); assert.ok(r.layer.grid); assert.equal(r.body._partBoxes().length,1);
  await turnDeviceChair({r},Math.PI);
  near(r.scene.objects[0].seat.yaw,Math.PI); near(r.scene.collisionParts[0].max[0],.2);
  assert.ok(calls.some(x=>x[0]==='remove')); assert.ok(r.rigid.world.items.has('floor'));
  near(r.rigid.world.items.get('desk_chair#0').max[2],.3);
  near(r.props.layout.devices.chairYaw,Math.PI);
});
test('chair rejects occupied chair, blocked sweep and unavailable physics without altering its pose', async()=>{
  const {r,chair}=chairFixture();prepareDeviceRoom(r);const q=chair.quaternion.clone();
  r.layer.seated={};await assert.rejects(()=>turnDeviceChair({r},Math.PI),/离开椅子/);r.layer.seated=null;
  r.layer.char.x=.1;await assert.rejects(()=>turnDeviceChair({r},Math.PI),/挡住/);r.layer.char.x=1.2;
  r.rigid={ready:Promise.resolve(),failed:'wasm'};await assert.rejects(()=>turnDeviceChair({r},Math.PI),/刚体/);
  assert.deepEqual(q.toArray(),chair.quaternion.toArray());
});
test('two room instances never share chair state; legacy room is untouched',()=>{
  const a=chairFixture(),b=chairFixture();a.r.props.layout.devices={chairYaw:0};prepareDeviceRoom(a.r);prepareDeviceRoom(b.r);
  near(a.r.scene.objects[0].seat.yaw,0);near(b.r.scene.objects[0].seat.yaw,Math.PI/2);
  assert.equal(prepareDeviceRoom({scene:{sceneId:'petal-oak-life-v1'}}),false);
});
test('lost contact and cancellation stop chair rotation and retain its actual partial pose',async()=>{
  const {r}=chairFixture();prepareDeviceRoom(r);const before=r.scene.objects[0].seat.yaw;let n=0;
  await assert.rejects(()=>turnDeviceChair({r},Math.PI,()=>++n<2),/手离开/);
  const actual=r.scene.objects[0].seat.yaw;assert.ok(actual>before&&actual<Math.PI);near(r.props.layout.devices.chairYaw,actual);
  await assert.rejects(()=>turnDeviceChair({r,hurry:true},Math.PI),/已停止/);near(r.scene.objects[0].seat.yaw,actual);
});
test('seated swivel rotates the real rider root, seat copy, action anchor and foot locks together',async()=>{
  const {r}=chairFixture();prepareDeviceRoom(r);const L=r.layer;
  L.hipHeight=.78;L.nodes={};for(const name of ['head','hips','leftLowerLeg','rightLowerLeg','leftFoot','rightFoot']){
    const n=new THREE.Object3D();n.position.set(.1,name==='head'?1.3:.5,0);L.nodes[name]=n;
  }
  const seat={...L.targets.desk_chair.seat,id:'desk_chair'};
  L.char={x:.1,z:0,yaw:Math.PI/2};L.seated={seat};L.action={loop:true,seat:{info:seat},anchor:{x:.1,z:0,yaw:Math.PI/2}};
  L.feet={left:{lock:new THREE.Vector3(.4,.05,0),seatT:new THREE.Vector3(.4,.05,0)}};
  await turnDeviceChair({r},Math.PI,null,true);
  near(L.char.x,0);near(L.char.z,-.1);near(L.char.yaw,Math.PI);near(L.action.anchor.z,-.1);
  near(seat.yaw,Math.PI);near(L.feet.left.lock.z,-.4);near(L.feet.left.seatT.z,-.4);
  await slideDeviceChair({r},[.3,0,0]);near(L.char.x,.3);near(L.action.anchor.x,.3);near(seat.pos[0],.3);
  near(r.scene.collisionParts[0].min[0],.1);near(r.scene.objects[0].seat.pos[0],.3);
  await slideDeviceChair({r},[0,0,0]);near(L.char.x,0);near(L.action.anchor.x,0);near(seat.pos[0],0);
});

function phoneFixture({pickup=false,returnFailure=false,viewFailure=false}={}){
  const mesh=new THREE.Mesh(new THREE.BoxGeometry(.08,.015,.16));mesh.position.set(1,.8,2);mesh.name='IR_Item_iPhone18ProMax';
  const item={id:mesh.name,mesh,held:null}, later=[],calls=[];
  const r={scene:{sceneId:'casa-luce-v8'},layer:{items:{[item.id]:item},targets:{},char:{x:1,z:2},lookAt:()=>{}},
    poses:{set:name=>calls.push(['pose',name])},props:{node:()=>mesh,asItem:()=>item.id,glb:{[item.id]:{node:mesh}},
      wake:()=>calls.push('wake'),settle:()=>calls.push('settle'),follow:()=>{},every:()=>()=>{},tween:async(d,fn)=>fn(1)}};
  const ctx={r,life:{},later:fn=>later.push(fn),say:()=>{},wait:async()=>{if(viewFailure)throw new Error('view interrupted');}};
  const scripts=createDeviceScripts({homeOf:()=>{r.layer.targets.home={};return 'home';},grab:async()=>{calls.push('pick');if(!pickup)item.held='right';},
    place:async()=>{calls.push('put');if(returnFailure)throw new Error('return blocked');item.held=null;}});
  return {r,ctx,item,later,calls,scripts};
}
test('phone success requires actual held state and return to origin, preserving selected hand',async()=>{
  const f=phoneFixture();await f.scripts.device_phone(f.ctx,{seconds:3});
  assert.equal(f.r.deviceLast.ok,true);assert.equal(f.r.deviceLast.hand,'right');assert.equal(f.item.held,null);
  assert.equal(f.r.layer.targets.home.itemId,f.item.id);assert.equal(f.r.deviceLast.returnError,0);
  assert.equal(f.r.deviceLast.osInput,false);assert.ok(f.calls.includes('put'));
});
test('phone failed pickup/return never produces success or resets/teleports the item',async()=>{
  const f=phoneFixture({pickup:true});await assert.rejects(()=>f.scripts.device_phone(f.ctx,{}),/实际拿到/);assert.equal(f.r.deviceLast.ok,false);
  const g=phoneFixture({returnFailure:true});await assert.rejects(()=>g.scripts.device_phone(g.ctx,{}),/return blocked/);
  assert.equal(g.r.deviceLast.ok,false);assert.equal(g.item.held,'right');
});
test('phone cleanup tries the same item home after interruption and never takes another held object',async()=>{
  const f=phoneFixture({viewFailure:true});await assert.rejects(()=>f.scripts.device_phone(f.ctx,{}),/interrupted/);
  for(const fn of f.later.reverse())await fn();assert.equal(f.item.held,null);assert.ok(f.calls.includes('put'));
  const g=phoneFixture();g.r.layer.items.other={held:'left'};
  await assert.rejects(()=>g.scripts.device_phone(g.ctx,{}),/先放好/);assert.deepEqual(g.calls,[]);
});
test('typing targets vary with time, stay near their own keyboard targets, and do not mutate them',()=>{
  const left=new THREE.Vector3(1,.8,2),right=new THREE.Vector3(1,.8,2.2);
  const c={opts:{left,right},left:new THREE.Vector3(0,0,1),t:0};
  const a=POSES.deviceTyping.ik.leftHand(c).target;c.t=Math.PI/18;const b=POSES.deviceTyping.ik.leftHand(c).target;
  near(a.x,b.x);near(a.z,b.z);assert.ok(b.y>a.y);near(left.y,.8);assert.ok(b.distanceTo(left)<.009);
});
test('installed V8 GLB contains actual semantic phone, chair, keyboard and screen; chair has no independent swivel node',()=>{
  const bytes=readFileSync(new URL('../../desktop/third_party/rooms/casa-luce-v8/room.glb',import.meta.url));
  const glb=JSON.parse(bytes.subarray(20,20+bytes.readUInt32LE(12)).toString('utf8'));
  for(const name of ['IR_Item_iPhone18ProMax','Asset_Chair','Asset_Keyboard','V8_Monitor_Preview'])assert.ok(glb.nodes.some(n=>n.name===name),name);
  const chair=glb.nodes.find(n=>n.name==='Asset_Chair');assert.equal(chair.children.length,4);
  assert.ok(chair.children.every(i=>Number.isInteger(glb.nodes[i].mesh)));
});
