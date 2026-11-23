import {test} from 'node:test';
import assert from 'node:assert/strict';
import {register} from 'node:module';
const THREE_URL=new URL('../../web/vendor/three/build/three.module.js',import.meta.url).href;
const STATIC_URL=new URL('../../web/',import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n){
  if(s==='three')return {url:${JSON.stringify(THREE_URL)},shortCircuit:true};
  if(s.startsWith('/static/'))return {url:new URL(s.slice(8),${JSON.stringify(STATIC_URL)}).href,shortCircuit:true};
  return n(s,c);}`)}`);
globalThis.location={search:''};
const T=await import('three');
const {PoseLayer,POSES}=await import('../../desktop/web/room-poses.js');

function fixture(){
  const scene=new T.Group(),nodes={};
  for(const n of ['hips','neck','head','rightUpperArm','rightLowerArm','rightHand']){nodes[n]=new T.Bone();scene.add(nodes[n]);}
  const calls=[];const layer={vrm:{scene,humanoid:{getNormalizedBoneNode:n=>nodes[n]}},char:{x:0,z:0,yaw:0},hipHeight:.78,
    _twoBone:(...a)=>calls.push(a)};
  const p=new PoseLayer(layer);p._palm=()=>{};
  return {p,calls};
}
test('sip exact solve is opt-in, happens in PoseLayer, and does not alter other IK call routes',()=>{
  for(const [name,opts,expected] of [['sip',{},undefined],['sip',{sipExact:true},true],['sip',{sipExact:'true'},undefined],['bite',{sipExact:true},undefined]]){
    const {p,calls}=fixture();p.set(name,opts);p._main(.033);
    assert.equal(calls.length,1);assert.equal(calls[0][5],expected);
    assert.equal(p._target.isVector3,true);assert.ok(p.W<1); // Existing temporal blend still owns the output.
  }
});

test('opted-in sip rotates the cup-facing palm toward the body by at most 30 degrees for either hand',()=>{
  for(const yaw of [0,.8,-1.7])for(const side of [-1,1]){
    const fwd=new T.Vector3(Math.sin(yaw),0,Math.cos(yaw)),left=new T.Vector3(Math.cos(yaw),0,-Math.sin(yaw));
    const context={fwd,left,side,opts:{reach:new T.Vector3()}};
    const normal=POSES.sip.ik.hand(context).palm;
    assert.ok(normal.distanceTo(left.clone().multiplyScalar(-side))<1e-12);
    context.opts={...context.opts,sipExact:true,sipPalmIn:Math.PI/6};
    const inward=POSES.sip.ik.hand(context).palm;
    assert.ok(Math.abs(inward.length()-1)<1e-12);
    assert.ok(Math.abs(inward.angleTo(normal)-Math.PI/6)<1e-12);
    assert.ok(Math.abs(inward.dot(fwd)+.5)<1e-12);
    context.opts.sipPalmIn=100;assert.ok(POSES.sip.ik.hand(context).palm.distanceTo(inward)<1e-12);
    context.opts.sipPalmIn=-1;assert.ok(POSES.sip.ik.hand(context).palm.distanceTo(normal)<1e-12);
    context.opts.sipPalmIn=NaN;assert.ok(POSES.sip.ik.hand(context).palm.distanceTo(normal)<1e-12);
    context.opts.sipPalmIn=Math.PI/6;context.opts.sipExact=false;
    assert.ok(POSES.sip.ik.hand(context).palm.distanceTo(normal)<1e-12);
  }
});
