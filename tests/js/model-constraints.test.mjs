import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';
// 'three' / '@pixiv/three-vrm' 是浏览器 importmap 里的裸包名 (three-vrm、face-rig、springs.js 里面也这样引):
// 自己挂上 local-vrm-loader, 照 README 的 node --test "tests/js/*.test.mjs" 跑就行, 不用另加 --loader (2026-11-01)
register('./local-vrm-loader.mjs', import.meta.url);
const THREE = await import('three');
const { VRMLookAtBoneApplier, VRMLookAtRangeMap } = await import('@pixiv/three-vrm');
const { FaceRig } = await import('../../web/vendor/soullink3d/dist/src/face-rig.js');
const { applyModelCorrectives, springFallbackKind } = await import('../../web/model-constraints.js');
globalThis.location = { search: '' };
const { bodyColliders, surfaceCollider } = await import('../../desktop/web/springs.js');

function fixture() {
  const scene = new THREE.Group(), bones = {};
  const add = (name, parent, y) => { const b = new THREE.Bone(); b.name = name; b.position.y = y; (bones[parent] ?? scene).add(b); bones[name] = b; return b; };
  add('hips', null, 0.8); add('spine','hips',0.15); add('chest','spine',0.15); add('neck','chest',0.1); add('head','neck',0.1);
  add('leftEye','head',0.05); add('rightEye','head',0.05);
  for (const s of ['left','right']) {
    add(s+'UpperLeg','hips',0); add(s+'LowerLeg',s+'UpperLeg',-0.4); add(s+'Foot',s+'LowerLeg',-0.4);
    add(s+'UpperArm','chest',0); add(s+'LowerArm',s+'UpperArm',-0.25); add(s+'Hand',s+'LowerArm',-0.2);
  }
  const humanoid = { humanBones: Object.fromEntries(Object.keys(bones).map(k=>[k,{}])), getRawBoneNode:n=>bones[n], getNormalizedBoneNode:n=>bones[n] };
  const joint = (parent) => { const bone=new THREE.Bone(); bones[parent].add(bone); return { bone, colliderGroups: [] }; };
  const hair=joint('head'), skirt=joint('hips'), accessory=joint('chest');
  const vrm={ scene, humanoid, meta:{metaVersion:'1'}, springBoneManager:{joints:new Set([hair,skirt,accessory])} };
  return {v:{vrm,scene},vrm,bones,hair,skirt,accessory};
}

test('FaceRig preserves all four authored input/output maps and reports physical limits',()=>{
  const {vrm}=fixture();
  const applier=Object.create(VRMLookAtBoneApplier.prototype);
  const names=['HorizontalInner','HorizontalOuter','VerticalUp','VerticalDown'];
  names.forEach((name,i)=>applier['rangeMap'+name]={inputMaxValue:90+i,outputScale:[6,6,4,4][i]});
  vrm.lookAt={applier,autoUpdate:true,faceFront:new THREE.Vector3(0,0,1)};
  const before=names.map(n=>({...applier['rangeMap'+n]}));
  const rig=new FaceRig(vrm);rig.apply({yaw:0,pitch:0,roll:0},{yaw:30,pitch:25});
  assert.deepEqual(names.map(n=>applier['rangeMap'+n]),before);
  assert.deepEqual(rig.capabilities.eyeOutputLimits,{inner:6,outer:6,up:4,down:4});
  assert.equal(rig.capabilities.eyeControl,'bone-authored-range');
  assert.equal(vrm.lookAt.yaw,91);assert.equal(vrm.lookAt.pitch,-93);
  assert.equal(vrm.lookAt.autoUpdate,false);
});

test('models without bone look-at still load',()=>{
  const {vrm}=fixture(); const rig=new FaceRig(vrm);
  assert.equal(rig.capabilities.eyeOutputLimits,null); assert.equal(rig.capabilities.eyeControl,'unavailable');
});

test('bundled bone applier respects authored SilverCat 6/4 degree limits at extremes',()=>{
  const {vrm,bones}=fixture();
  const applier=new VRMLookAtBoneApplier(vrm.humanoid,new VRMLookAtRangeMap(45,6),new VRMLookAtRangeMap(45,6),new VRMLookAtRangeMap(45,4),new VRMLookAtRangeMap(45,4));
  vrm.lookAt={applier,faceFront:new THREE.Vector3(0,0,1)};const rig=new FaceRig(vrm);
  for(const yaw of [-900,-90,-30,0,30,90,900]) for(const pitch of [-900,-90,-25,0,25,90,900]) {
    applier.applyYawPitch(yaw,pitch);
    for(const name of ['leftEye','rightEye']){
      const e=new THREE.Euler().setFromQuaternion(bones[name].quaternion,'YXZ');
      assert.ok(Math.abs(THREE.MathUtils.radToDeg(e.y))<=6+1e-8);
      assert.ok(Math.abs(THREE.MathUtils.radToDeg(e.x))<=4+1e-8);
    }
  }
  for (const scale of [0,.25,.5,1]) {
    rig.apply({yaw:0,pitch:0,roll:0},{yaw:30*scale,pitch:25*scale});
    applier.applyYawPitch(vrm.lookAt.yaw,vrm.lookAt.pitch);
    const e=new THREE.Euler().setFromQuaternion(bones.leftEye.quaternion,'YXZ');
    assert.ok(Math.abs(THREE.MathUtils.radToDeg(e.y)-6*scale)<1e-8);
    assert.ok(Math.abs(THREE.MathUtils.radToDeg(e.x)+4*scale)<1e-8);
  }
});

test('final per-mesh corrective products do not attenuate base inputs or leak between meshes',()=>{
  const names=['eyeBlinkLeft','eyeWideLeft','jawOpen','mouthClose','corr_eyeBlinkWideLeft','corr_eyeBlinkWideRight','corr_jawOpenMouthClose'];
  const mk=(a,b,c,d)=>({morphTargetDictionary:Object.fromEntries(names.map((n,i)=>[n,i])),morphTargetInfluences:[a,b,c,d,99,99,99]});
  const a=mk(1,0.8,0.75,1),b=mk(0.5,0.3,0.8,0.2),plain={morphTargetDictionary:{jawOpen:0},morphTargetInfluences:[0.6]};
  applyModelCorrectives([a,b,plain]);
  assert.deepEqual(a.morphTargetInfluences,[1,0.8,0.75,1,0.8,0,0.75]);
  assert.equal(b.morphTargetInfluences[4],0.15);assert.ok(Math.abs(b.morphTargetInfluences[6]-0.16)<1e-12);
  assert.deepEqual(plain.morphTargetInfluences,[0.6]);
  a.morphTargetInfluences[0]=0;a.morphTargetInfluences[3]=0;applyModelCorrectives([a]);
  assert.equal(a.morphTargetInfluences[4],0);assert.equal(a.morphTargetInfluences[6],0);
});

test('corrective weights clamp invalid/out-of-range source values',()=>{
  const mesh={morphTargetDictionary:{eyeBlinkLeft:0,eyeWideLeft:1,corr_eyeBlinkWideLeft:2},morphTargetInfluences:[2,0.5,0]};
  applyModelCorrectives([mesh]);assert.equal(mesh.morphTargetInfluences[2],0.5);
  mesh.morphTargetInfluences[0]=NaN;applyModelCorrectives([mesh]);assert.equal(mesh.morphTargetInfluences[2],0);
});

test('all authored groups, shared references and intentional arm colliders survive',()=>{
  const {v,vrm,bones,hair,skirt}=fixture();
  const arm=new THREE.Object3D();bones.leftUpperArm.add(arm);
  const group={name:'authored',colliders:[arm]}, hg=[group],sg=[group];hair.colliderGroups=hg;skirt.colliderGroups=sg;
  assert.equal(springFallbackKind(vrm,skirt),null);assert.equal(bodyColliders(v),0);
  assert.equal(hair.colliderGroups,hg);assert.equal(skirt.colliderGroups,sg);assert.equal(group.colliders[0],arm);
  assert.equal(bodyColliders(v),0);assert.equal(bones.head.children.filter(c=>c.isVRMSpringBoneCollider).length,0);
});

test('fallback hair includes arms; fallback skirts exclude only newly synthesized arms',()=>{
  const {v,bones,hair,skirt,accessory}=fixture();
  assert.equal(bodyColliders(v),12);
  const hg=hair.colliderGroups[0],sg=skirt.colliderGroups[0];
  assert.equal(hg.colliders.length,12);assert.equal(sg.colliders.length,8);assert.equal(accessory.colliderGroups.length,0);
  assert.ok(hg.colliders.some(c=>c.parent===bones.leftUpperArm));assert.ok(!sg.colliders.some(c=>/Arm/.test(c.parent.name)));
  assert.equal(bodyColliders(v),0);assert.equal(hair.colliderGroups.length,1);
});

test('surface contact remains available with authored bodies and is idempotent',()=>{
  const {v,bones,hair,skirt}=fixture();const group={name:'authored',colliders:[bones.leftUpperArm]};
  hair.colliderGroups=[group];skirt.colliderGroups=[group];bodyColliders(v);
  const api=surfaceCollider(v);assert.equal(surfaceCollider(v),api);
  assert.equal(hair.colliderGroups.length,2);assert.equal(hair.colliderGroups[0],group);
  api.set(new THREE.Vector3(0,0.5,0),0.6);api.set(new THREE.Vector3(0,0.6,0),0.7);
  assert.equal(v.vrm.springBoneManager.__surfaceGroup.colliders.length,1);
  assert.equal(group.colliders.length,1);api.set(null);assert.equal(v.vrm.springBoneManager.__surfaceGroup.colliders.length,0);
});
