import {test} from 'node:test';
import assert from 'node:assert/strict';
import {solveTwoBone,solveForearmTwist,wristAngles,scoreStaticGrasp} from '../../web/grasp-constraint-solver.js';
const rad=d=>d*Math.PI/180;
const q=(axis,d)=>[...axis.map(x=>x*Math.sin(rad(d)/2)),Math.cos(rad(d)/2)];
const mul=(a,b)=>{const[x,y,z,w]=a,[u,v,t,s]=b;return[w*u+x*s+y*t-z*v,w*v-x*t+y*s+z*u,w*t+x*v-y*u+z*s,w*s-x*u-y*v-z*t];};
const close=(a,b,t=1e-9)=>assert.ok(Math.abs(a-b)<=t,`${a} != ${b}`);
const distance=(a,b)=>Math.hypot(...a.map((x,i)=>x-b[i]));

test('exact two-bone solve preserves both segment lengths through near-straight poses and swivels',()=>{
  for(const d of [.02,.2,.378845188456,.388864387212-1e-5])for(const swivel of [-2,-.5,0,1,2.5]){
    const a=.202092616754,b=.186771770458,root=[1,.7,-2],end=[1+d,.7,-2];
    const s=solveTwoBone({root,end,upperLength:a,lowerLength:b,pole:[1,0,-1],swivel});
    assert.equal(s.reachable,true);close(distance(root,s.joint),a);close(distance(end,s.joint),b);
    assert.ok(s.elbowDegrees>=0&&s.elbowDegrees<=180);assert.equal(s.residual,0);
  }
});
test('the measured batch3 target is rejected as unreachable, without inventing a successful clamped target',()=>{
  const s=solveTwoBone({root:[-2.2764957333,1.1969069886,2.1412386644],end:[-2.6266105536,.667962663,2.0821473598],
    upperLength:.202092616754,lowerLength:.186771770458,pole:[-2.3,.7,2]});
  assert.equal(s.reachable,false);close(s.residual,.24820254,1e-6);assert.equal(s.joint,null);
});
test('inner reach boundary, singular endpoints and degenerate poles are explicit',()=>{
  assert.equal(solveTwoBone({root:[0,0,0],end:[.1,0,0],upperLength:.4,lowerLength:.2,pole:[0,1,0]}).reachable,false);
  assert.equal(solveTwoBone({root:[0,0,0],end:[0,0,0],upperLength:.2,lowerLength:.2,pole:[0,1,0]}).reason,'coincident-endpoints');
  const s=solveTwoBone({root:[0,0,0],end:[.3,0,0],upperLength:.2,lowerLength:.2,pole:[1,0,0]});
  assert.equal(s.fallbackPole,true);close(distance([0,0,0],s.joint),.2);
});
test('forearm twist can distribute an excessive wrist deviation into permitted wrist flexion',()=>{
  const hand=q([0,1,0],70);assert.ok(wristAngles(hand).deviationDegrees < -35);
  const s=solveForearmTwist({parentWorldQuaternion:[0,0,0,1],forearmLocalQuaternion:[0,0,0,1],handWorldQuaternion:hand});
  assert.equal(s.feasible,true);assert.ok(s.intervals.length);assert.ok(Math.abs(s.deltaDegrees)>70);
  assert.ok(s.forearmTwistDegrees>=-85-1e-7&&s.forearmTwistDegrees<=80+1e-7);
  assert.ok(s.flexDegrees>=-75-1e-7&&s.flexDegrees<=75+1e-7);assert.ok(s.deviationDegrees>=-35-1e-7&&s.deviationDegrees<=20+1e-7);
});
test('forearm twist never licenses a backwards wrist or a restricted impossible twist',()=>{
  const s=solveForearmTwist({parentWorldQuaternion:[0,0,0,1],forearmLocalQuaternion:[0,0,0,1],handWorldQuaternion:q([0,1,0],150)});
  assert.equal(s.feasible,false);assert.equal(s.reason,'wrist-points-backward');
  const t=solveForearmTwist({parentWorldQuaternion:[0,0,0,1],forearmLocalQuaternion:[0,0,0,1],handWorldQuaternion:q([0,1,0],70),
    limits:{forearm:{sup:10,pron:10},wrist:{flex:75,ext:75,ulnar:35,radial:20}}});
  assert.equal(t.feasible,false);
});
test('analytic twist intervals include a known legal solution for both sides under arbitrary world rotations',()=>{
  for(const side of ['left','right'])for(const initial of [-40,0,35])for(const wanted of [-60,0,60]){
    const sign=side==='left'?1:-1,out=[sign,0,0],parent=mul(q([0,1,0],31),q([0,0,1],-23));
    const local=q(out,initial),desired=mul(mul(parent,q(out,wanted)),q([0,0,1],20));
    const s=solveForearmTwist({parentWorldQuaternion:parent,forearmLocalQuaternion:local,handWorldQuaternion:desired,side});
    assert.equal(s.feasible,true,JSON.stringify({side,initial,wanted,s}));
    assert.ok(s.examined<40,'search enumerates constraint boundaries, not a dense angle grid');
  }
});
test('zero additional twist is retained when all active constraints already pass',()=>{
  const local=q([1,0,0],30),s=solveForearmTwist({parentWorldQuaternion:[0,0,0,1],forearmLocalQuaternion:local,
    handWorldQuaternion:mul(local,q([0,0,1],20))});
  assert.equal(s.feasible,true);close(s.delta,0);
});
const good={palmPosition:.005,palmAngle:rad(5),footPosition:.001,footAngle:rad(1),romExcessDegrees:0,collisionDepth:0,headDepth:0,balanceMargin:.01};
test('all hard constraints must pass: contact and head clearance cannot compensate for bad feet, ROM or collisions',()=>{
  assert.equal(scoreStaticGrasp(good).feasible,true);
  for(const[name,value]of [['footPosition',.02],['footAngle',rad(20)],['romExcessDegrees',10],['collisionDepth',.002],['balanceMargin',-.006]]){
    const s=scoreStaticGrasp({...good,[name]:value});assert.equal(s.feasible,false);assert.ok(s.violations.some(v=>v.name===name));
  }
});
test('missing/nonfinite evidence fails closed; support is required for put',()=>{
  for(const value of [undefined,NaN,Infinity])assert.equal(scoreStaticGrasp({...good,collisionDepth:value}).evidenceComplete,false);
  assert.equal(scoreStaticGrasp(good,{requireSupport:true}).feasible,false);
  assert.equal(scoreStaticGrasp({...good,supportGap:.002},{requireSupport:true}).feasible,true);
  assert.equal(scoreStaticGrasp({...good,supportGap:-.02},{requireSupport:true}).feasible,false);
});

test('negative unsigned measurements or thresholds are invalid evidence, never a feasible shortcut',()=>{
  for(const name of ['palmPosition','palmAngle','footPosition','footAngle','romExcessDegrees','collisionDepth','headDepth']){
    const result=scoreStaticGrasp({...good,[name]:-.001});
    assert.equal(result.feasible,false);assert.equal(result.evidenceComplete,false);assert.deepEqual(result.invalid,[name]);
    assert.equal(result.violationCost,null);
  }
  assert.equal(scoreStaticGrasp(good,{limits:{palmPosition:-1}}).feasible,false);
  assert.equal(scoreStaticGrasp(null).evidenceComplete,false);
  assert.equal(scoreStaticGrasp({...good,balanceMargin:-.1}).evidenceComplete,true);
  assert.equal(scoreStaticGrasp({...good,supportGap:-.002},{requireSupport:true}).feasible,true);
});
