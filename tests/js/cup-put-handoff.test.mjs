import {test} from 'node:test';
import assert from 'node:assert/strict';
import {register} from 'node:module';
const THREE_URL=new URL('../../web/vendor/three/build/three.module.js',import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n){return s==='three'?{url:${JSON.stringify(THREE_URL)},shortCircuit:true}:n(s,c);}
export async function load(u,c,n){const r=await n(u,c);return u.endsWith('/web/locomotion.js')?{...r,source:r.source+'\\nexport { Locomotion };'}:r;}`)}`);
const T=await import('three');
const {Locomotion,TUNE}=await import('../../web/locomotion.js');
function fixture(side='right'){
 const scene=new T.Group(),hips=new T.Bone(),up=new T.Bone(),lo=new T.Bone(),hand=new T.Bone();scene.add(hips);hips.add(up);up.add(lo);lo.add(hand);hips.position.y=.8;hand.position.set(.2,.2,.3);scene.updateMatrixWorld(true);
 const it={held:side,grip:'cup',mesh:{geometry:{type:'CylinderGeometry'}}};
 const L=Object.create(Locomotion.prototype);Object.assign(L,{items:{it},nodes:{hips,[side+'UpperArm']:up,[side+'LowerArm']:lo,[side+'Hand']:hand},vrm:{scene},hipHeight:.8,carryCare:1,handSpeed:.6,
  char:{x:0,z:0,yaw:0},sim:{x:0,z:0,yaw:0},pointing:{w:0},carryW:{left:1,right:1},carryU:{left:1,right:1},_carryWset:{left:1,right:1},acts:{fps:30},_anchor(){},_grabHint:()=>new T.Vector3(0,-1,0),_holdTarget:(s,h)=>h.center.clone(),_twoBone(){}});
 return {L,it,scene,hand};
}
test('cup put origin captures the displayed wrist before root warp, for either hand and each new put',()=>{
 for(const side of ['left','right']){const {L,hand,scene}=fixture(side);const from=L._cupPutFrom(side,'put');assert.ok(from.distanceTo(hand.getWorldPosition(new T.Vector3()))<1e-12);
  scene.position.x+=.25;scene.updateMatrixWorld(true);assert.ok(Math.abs(L._cupPutFrom(side,'put').x-from.x-.25)<1e-12);assert.equal(from.x,.2);}
});
test('non-cup, physical, ordinary carry, wrong side and pick retain old origins',()=>{
 for(const change of [({it})=>it.grip='book',({it})=>it.grip='ball',({it})=>it.physical=true,({L})=>L.carryCare=0,({it})=>it.held='left']){const f=fixture();change(f);assert.equal(f.L._cupPutFrom('right','put'),null);}
 const {L}=fixture();assert.equal(L._cupPutFrom('right','pick'),null);
});
test('actual carry path keeps captured start independent of root drift and reaches the unchanged hold endpoint',()=>{
 for(const side of ['left','right']){
  const {L}=fixture(side),from=L._cupPutFrom(side,'put'),end=new T.Vector3(.35,.6,.65);
  const grab={side,kind:'put',carryFrom:from,hold:{center:end}};L.action={grab,start:0,event:30,frame:0,fired:false,loop:false};
  for(const shift of [0,.07,.15]){L.char.x=shift;L.sim.x=shift;L._carry(1/30);assert.ok(L._carryTarget[side].target.distanceTo(from)<1e-10);}
  L.action.frame=30;L._carry(1/30);assert.ok(L._carryTarget[side].target.distanceTo(end)<1e-10);assert.ok(grab.carryFrom.distanceTo(from)<1e-12);
 }
});

test('exact pickup solve is limited to the new bound cup during stationary handoff',()=>{
 for(const mode of ['idle','act']){
  const f=fixture(),{L,it}=f;it.size=new T.Vector3(.09,.07,.06);it.cupBinding=true;L.mode=mode;L.path=null;
  if(mode==='act')L.action={grab:{side:'right',kind:'pick',hold:{cupTask:{bound:true}}},fired:true,loop:false};
  L._pickHand={right:{from:L._cupPutFrom('right','put'),t:0}};const calls=[];L._twoBone=(...args)=>calls.push(args[5]);
  L._carry(1/30);assert.equal(calls[0],true);
 }
});
test('ordinary cup, physical, book, walking and ended handoff keep the default solve',()=>{
 const changes=[({it})=>delete it.cupBinding,({it})=>it.physical=true,({it})=>it.grip='book',({L})=>L.mode='go',({L})=>L.path=[[0,0],[1,0]],({L})=>L._pickHand.right.t=100];
 for(const change of changes){
  const f=fixture(),{L,it}=f;it.size=new T.Vector3(.09,.07,.06);it.cupBinding=true;L.mode='idle';L.path=null;L._pickHand={right:{from:L._cupPutFrom('right','put'),t:0}};change(f);
  const calls=[];L._twoBone=(...args)=>calls.push(args[5]);L._carry(1/30);assert.notEqual(calls[0],true);
 }
});
