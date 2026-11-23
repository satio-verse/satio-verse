import {test} from 'node:test';
import assert from 'node:assert/strict';
import {register} from 'node:module';
const THREE_URL=new URL('../../web/vendor/three/build/three.module.js',import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n){return s==='three'?{url:${JSON.stringify(THREE_URL)},shortCircuit:true}:n(s,c);}`)}`);
const T=await import('three');
const {cupCurve,cupControlWeight,cupElbow,captureCupHandoff,applyCupHandoff}=await import('../../web/cup-handoff.js');
function rig(side){
 const root=new T.Group(),up=new T.Bone(),lo=new T.Bone(),hand=new T.Bone();root.add(up);up.add(lo);lo.add(hand);
 up.position.set(side==='left'?.1:-.1,1.2,0);lo.position.z=.22;hand.position.z=.17;up.rotation.x=-.4;lo.rotation.x=1;root.updateMatrixWorld(true);
 const L={nodes:{[side+'UpperArm']:up,[side+'LowerArm']:lo,[side+'Hand']:hand},_world:n=>n.getWorldPosition(new T.Vector3())};
 return {L,up,lo,hand,root};
}
test('cup handoff curve and tail ownership have zero endpoint derivatives and exactly return to old carry',()=>{
 assert.equal(cupCurve(0),0);assert.equal(cupCurve(1),1);assert.equal(cupControlWeight(0),1);assert.equal(cupControlWeight(1),0);
 const h=1e-5;assert.ok((1-cupControlWeight(h))/h<1e-6);assert.ok(cupControlWeight(1-h)/h<1e-6);
});
test('analytic elbow obeys actual link lengths, clamps unreachable targets and handles straight/degenerate poles',()=>{
 const s=new T.Vector3(),p=new T.Vector3(0,-1,0),h=new T.Vector3(0,-1,0);
 for(const target of [new T.Vector3(.2,.1,.1),new T.Vector3(0,0,.389),new T.Vector3(0,0,.7)]){
  const x=cupElbow(s,target,.22,.17,p,h,.3);assert.ok(x);assert.ok(Math.abs(x.elbow.length()-.22)<1e-10);assert.ok(Math.abs(x.elbow.distanceTo(x.wrist)-.17)<1e-10);
 }
 assert.ok(cupElbow(s,new T.Vector3(0,0,.3),.22,.17,new T.Vector3(0,0,1),h,0));
 assert.equal(cupElbow(s,new T.Vector3(),.22,.17,p,h),null);
 assert.equal(cupElbow(s,new T.Vector3(0,0,.3),.22,.17,new T.Vector3(0,0,1),new T.Vector3(0,0,1)),null);
 assert.equal(cupElbow(s,new T.Vector3(0,0,.3),Infinity,.17,p,h),null);
});
test('both hands start at their measured binding pose without position or rotation jump',()=>{
 for(const side of ['left','right']){const {L,up,lo,hand}=rig(side),saved=captureCupHandoff(L,side),q=[up,lo,hand].map(n=>n.getWorldQuaternion(new T.Quaternion())),w=L._world(hand);
  const ph={cupPose:saved,phase:0,u:0};assert.equal(applyCupHandoff(L,side,ph,w,new T.Vector3(0,-1,0),new T.Vector3(1,0,0)),true);
  assert.ok(L._world(hand).distanceTo(w)<1e-10);[up,lo,hand].forEach((n,i)=>assert.ok(n.getWorldQuaternion(new T.Quaternion()).angleTo(q[i])<1e-7));
 }
});
test('world references remove changing clip elbow baselines while leaving reachable wrist exact before constraints',()=>{
 for(const side of ['left','right']){const {L,up,lo,hand,root}=rig(side),saved=captureCupHandoff(L,side),target=L._world(hand).add(new T.Vector3(.02,-.02,-.03)),ph={cupPose:saved,phase:.4,u:cupCurve(.4)};const results=[];
  for(const angle of [-.3,.4]){up.rotation.set(angle,.2,0);lo.rotation.set(.7,0,.2);root.updateMatrixWorld(true);applyCupHandoff(L,side,ph,target,new T.Vector3(0,-1,0),new T.Vector3(1,0,0));results.push([L._world(lo),L._world(hand),hand.getWorldQuaternion(new T.Quaternion())]);assert.ok(results.at(-1)[1].distanceTo(target)<1e-10);}
  assert.ok(results[0][0].distanceTo(results[1][0])<1e-10);assert.ok(results[0][2].angleTo(results[1][2])<1e-7);
 }
});
test('completed ownership does not mutate the original carry pose',()=>{
 const {L,up,lo,hand}=rig('right'),ph={cupPose:captureCupHandoff(L,'right'),phase:1,u:1},before=[up,lo,hand].map(n=>n.quaternion.toArray());
 assert.equal(applyCupHandoff(L,'right',ph,new T.Vector3(),new T.Vector3(0,-1,0),new T.Vector3(1,0,0)),false);
 assert.deepEqual([up,lo,hand].map(n=>n.quaternion.toArray()),before);
});

test('ownership curve is continuous on both sides of the tail boundary',()=>{
 const e=1e-6;assert.equal(cupControlWeight(.75-e),1);assert.ok(Math.abs(cupControlWeight(.75+e)-1)<1e-10);
 assert.equal(cupControlWeight(1+e),0);assert.ok(cupControlWeight(1-e)<1e-10);
});
test('opposite hint remains finite and preserves link lengths for either side',()=>{
 for(const sign of [-1,1]){const s=new T.Vector3(),target=new T.Vector3(0,0,.3),p=new T.Vector3(sign,0,0),h=p.clone().negate();
  const x=cupElbow(s,target,.22,.17,p,h,.5);assert.ok(x);assert.ok(x.elbow.toArray().every(Number.isFinite));assert.ok(Math.abs(x.elbow.distanceTo(x.wrist)-.17)<1e-10);
 }
});
