import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';
const THREE_URL = new URL('../../web/vendor/three/build/three.module.js', import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n) {
  return s==='three'?{url:${JSON.stringify(THREE_URL)},shortCircuit:true}:n(s,c); }`)}`);
const THREE = await import('three');
const C = await import('../../web/grasp-controller.js');
const M = await import('../../web/grasp-transform.js');

function fixture() {
  const scene = new THREE.Group(), parent = new THREE.Group(), hand = new THREE.Group(), middle = new THREE.Group(), head=new THREE.Group();
  scene.add(head);head.position.set(5,5,5);
  scene.add(parent); parent.position.set(1, .5, -.4); parent.rotation.y = .5;
  scene.add(hand); hand.position.set(.4, .9, -.2); hand.add(middle); middle.position.set(.08, 0, 0);
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(.3, .24, .05)); parent.add(mesh);
  const item = { id: 'book', mesh, half: .12, held: null, on: 'shelf', graspHalfExtents: [.15,.12,.025],
    grasp: { version: 1, point: [.15,0,0], normal: [1,0,0], up: [0,1,0], preferredHand: 'left',
      positionTolerance: .025, angleTolerance: Math.PI/18, supportTolerance: .003, putAngleTolerance: Math.PI/60 } };
  const L = { nodes: { leftHand: hand,head }, hipHeight:.78,graspHeadBoxes:()=>[],vrm: { humanoid: { getNormalizedBoneNode: () => middle } }, items: { book: item },
    _world: n => n.getWorldPosition(new THREE.Vector3()), _handPos: () => hand.getWorldPosition(new THREE.Vector3()),
    _grabGap: (...args) => { L.gaps.push(args); }, gaps: [], _anchor() {},
    // Position-only mock IK still retains real palm and parent quaternion transforms.
    _armTo: (side, p, w) => { hand.position.lerp(p, w); hand.updateWorldMatrix(true,true); } };
  L._graspBind = (it, side) => C.bindGrasp(L,it,side);
  L._graspRelease = (it, target, on) => C.releaseGrasp(L,it,target,on);
  C.setWorldPose(mesh, { position: [.2,.8,.3], quaternion: [0,0,0,1] });
  scene.updateMatrixWorld(true);
  return { L, item, hand, parent, mesh };
}
function close(a,b,tol=1e-9) { assert.ok(Math.abs(a-b)<=tol, `${a} != ${b}`); }
function samePose(a,b) {
  a.position.forEach((x,i)=>close(x,b.position[i]));
  close(new THREE.Quaternion(...a.quaternion).angleTo(new THREE.Quaternion(...b.quaternion)),0,1e-7);
}
function touch(f) { C.aimPalm(f.L,'left',M.targetPalm(C.worldPose(f.mesh),f.item.grasp)); }
function task(f,kind='pick') {
  const t = { kind,item:f.item,side:'left',grasp:M.validateGrasp(f.item.grasp),targetPose:C.worldPose(f.mesh),
    wait:0,ready:true,finished:false,surfaceY:.68,on:'shelf',binding:f.item.graspBinding?.pose };
  f.L._graspTask=t; f.L.reaching={hold:{graspTask:t},fired:false}; return t;
}

test('surface aim aligns the physical palm, including object/hand parent rotations',()=>{
  const f=fixture(), desired=M.targetPalm(C.worldPose(f.mesh),f.item.grasp); touch(f);
  samePose(C.palmPose(f.L,'left'),desired);
  const local=f.mesh.position.clone(); samePose(C.worldPose(f.mesh),{position:[.2,.8,.3],quaternion:[0,0,0,1]});
  assert.ok(local.distanceTo(new THREE.Vector3(.2,.8,.3))>.1,'proxy parent is nonidentity');
});
test('binding preserves object pose exactly and tracks the actual palm rigidly',()=>{
  const f=fixture();touch(f); const before=C.worldPose(f.mesh);
  C.bindGrasp(f.L,f.item,'left');samePose(C.worldPose(f.mesh),before);
  assert.equal(f.item.held,'left'); assert.equal(f.L.gaps.length,1); assert.equal(f.L.gaps[0][3],true);
  f.hand.position.x+=.1;f.hand.rotateZ(.4);C.syncGrasp(f.L,f.item);
  samePose(C.worldPose(f.mesh),M.poseFromPalm(C.palmPose(f.L,'left'),f.item.graspBinding.pose));
});
test('air grabs, wrong orientation, occupied preferred hand and physical items are rejected',()=>{
  const f=fixture(); assert.throws(()=>C.bindGrasp(f.L,f.item,'left'),/接触/);
  touch(f);f.hand.rotateY(.4);assert.throws(()=>C.bindGrasp(f.L,f.item,'left'),/接触/);
  touch(f);f.L.items.other={held:'left'};assert.throws(()=>C.bindGrasp(f.L,f.item,'left'),/接触/);
  delete f.L.items.other;f.item.physical=true;assert.equal(C.usesGrasp(f.item),false);
  assert.throws(()=>C.bindGrasp(f.L,f.item,'left'),/绑定/);assert.equal(f.item.held,null);
});
test('finalization checks the final pose, waits for convergence, then binds once',()=>{
  const f=fixture();touch(f);const t=task(f); f.hand.position.x+=.05;
  C.finalizeGrasp(f.L,.1);assert.equal(f.item.held,null);assert.equal(f.L.reaching.fired,false);
  touch(f);C.finalizeGrasp(f.L,.1);assert.equal(f.item.held,'left');assert.equal(t.succeeded,true);
  C.finalizeGrasp(f.L,.1);assert.equal(f.L.gaps.length,1);assert.equal(f.L.lastGraspContact.outcome,'contact');
});
test('unreachable final contact fails after bounded wait without binding or moving object',()=>{
  const f=fixture(),before=C.worldPose(f.mesh),t=task(f);
  C.finalizeGrasp(f.L,1.21);assert.equal(f.L.reaching.fired,true);assert.ok(t.error);
  assert.equal(f.item.held,null);samePose(C.worldPose(f.mesh),before);assert.equal(f.L.lastGraspContact.outcome,'unreachable');
});
test('put rejects hovering or tilted bottom corners even inside the pick position tolerance',()=>{
  const f=fixture();touch(f);C.bindGrasp(f.L,f.item,'left');const t=task(f,'put');
  f.hand.position.y+=.01;C.syncGrasp(f.L,f.item);
  let e=C.contactReport(f.L,t);assert.ok(e.position<.025);assert.equal(e.ok,false);close(e.support,.01);
  assert.throws(()=>C.releaseGrasp(f.L,f.item,t.targetPose,'shelf'),/支撑/);
  C.aimPalm(f.L,'left',M.palmForObject(t.targetPose,t.binding));C.syncGrasp(f.L,f.item);
  f.hand.rotateZ(.06);C.syncGrasp(f.L,f.item);e=C.contactReport(f.L,t);assert.equal(e.ok,false);
});
test('put releases at the actual pose without snapping and keeps its target through fade-out',()=>{
  const f=fixture();touch(f);C.bindGrasp(f.L,f.item,'left');const t=task(f,'put');
  // A valid sub-millimetre residual remains visible after release; no target assignment.
  f.hand.position.y+=.001;C.syncGrasp(f.L,f.item);const before=C.worldPose(f.mesh),desired=C.taskPalm(t);
  C.finalizeGrasp(f.L,.1);assert.equal(f.item.held,null);assert.equal(f.item.graspBinding,null);
  samePose(C.worldPose(f.mesh),before);samePose(C.taskPalm(t),desired);assert.equal(f.L.gaps.length,2);
});
test('direct held-side mutation is rejected rather than silently transferring the attachment',()=>{
  const f=fixture();touch(f);C.bindGrasp(f.L,f.item,'left');f.item.held='right';
  assert.throws(()=>C.syncGrasp(f.L,f.item),/换手/);
});
test('unknown support geometry cannot pass a release check',()=>{
  const f=fixture();touch(f);C.bindGrasp(f.L,f.item,'left');const t=task(f,'put');
  delete f.item.graspHalfExtents;assert.equal(C.contactReport(f.L,t).ok,false);
  assert.throws(()=>C.releaseGrasp(f.L,f.item,t.targetPose,'shelf'),/支撑/);
});
test('a hand becoming occupied while approaching fails the final transition without crashing',()=>{
  const f=fixture();touch(f);const t=task(f);f.L.items.other={held:'left'};
  assert.doesNotThrow(()=>C.finalizeGrasp(f.L,.1));assert.equal(f.item.held,null);
  assert.equal(f.L.reaching.fired,true);assert.ok(t.error);assert.equal(t.succeeded,undefined);
});
test('home itemId routes to its held book instead of the other-hand cup, and never falls back',()=>{
  const book={held:'left'}, cup={held:'right'}, items={book,cup};
  assert.equal(C.putItem(items,{itemId:'book'}),book);
  assert.equal(C.putItem(items,{itemId:'missing'}),null);
  book.held=null;assert.equal(C.putItem(items,{itemId:'book'}),null);
  assert.equal(C.putItem(items,{itemId:null}),null);
  assert.equal(C.putItem(items,{}),cup);assert.equal(C.putItem(items,null),cup);
  cup.held=null;book.held='left';assert.equal(C.putItem(items,{}),book);
});
test('finalization requests same-frame visible sync only for explicit bindings and transitions',()=>{
  const f=fixture();assert.equal(C.finalizeGrasp(f.L,.1),false);
  f.L.items.ordinary={held:'right'};assert.equal(C.finalizeGrasp(f.L,.1),false);
  touch(f);task(f);assert.equal(C.finalizeGrasp(f.L,.1),true,'new binding must trigger follow');
  f.L._graspTask=null;assert.equal(C.finalizeGrasp(f.L,.1),true,'held final pose must trigger follow');
  task(f,'put');assert.equal(C.finalizeGrasp(f.L,.1),true,'release must trigger follow even after held clears');
  f.L._graspTask=null;assert.equal(C.finalizeGrasp(f.L,.1),false);
});
test('cancelling a pending final-pose contact prevents a late bind and settles only once',()=>{
  const f=fixture();touch(f);const t=task(f);let finishes=0;t.finish=()=>finishes++;
  assert.equal(C.cancelGrasp(f.L,'stopped'),true);assert.equal(C.cancelGrasp(f.L,'again'),false);
  assert.equal(finishes,1);assert.equal(t.finished,true);assert.equal(t.ready,false);
  assert.equal(f.L.reaching,null);assert.equal(f.L._graspTask,null);assert.equal(t.error,'stopped');
  C.finalizeGrasp(f.L,.1);assert.equal(f.item.held,null);
});
test('cancelling after binding or during put preserves the actual attachment and object pose',()=>{
  const f=fixture();touch(f);C.bindGrasp(f.L,f.item,'left');const t=task(f,'put');
  const binding=f.item.graspBinding,before=C.worldPose(f.mesh);let finishes=0;t.finish=()=>finishes++;
  C.cancelGrasp(f.L,'cleanup');assert.equal(finishes,1);assert.equal(f.item.held,'left');
  assert.equal(f.item.graspBinding,binding);samePose(C.worldPose(f.mesh),before);
  f.hand.position.x+=.1;C.finalizeGrasp(f.L,.1);
  samePose(C.worldPose(f.mesh),M.poseFromPalm(C.palmPose(f.L,'left'),binding.pose));
});
test('reset invalidates the old operation silently so deferred callbacks cannot finish it later',()=>{
  const f=fixture(),t=task(f);let finishes=0;t.finish=()=>finishes++;
  C.cancelGrasp(f.L,'reset',false);assert.equal(finishes,0);assert.equal(t.finished,true);
  assert.equal(f.L._graspTask,null);assert.equal(f.L.reaching,null);
  assert.equal(C.cancelGrasp(f.L,'late'),false);assert.equal(finishes,0);
});
test('final head gate uses rotated offset and room body softness, not the upright estimate',()=>{
  const f=fixture(),head=f.L.nodes.head;
  head.position.set(0,.5,0);head.rotation.z=-Math.PI/2;
  f.L.graspHeadBoxes=()=>[{id:'shelf',center:[.15,0],size:[.1,.4],y0:0,y1:1,soft:.015}];
  const h=C.headContact(f.L);close(h.position[0],.09);close(h.position[1],.5);close(h.depth,.075);
  assert.equal(h.source,'room-body');assert.equal(h.obstacle,'shelf');
  touch(f);const t=task(f);C.finalizeGrasp(f.L,1.21);assert.equal(f.item.held,null);
  assert.match(t.error,/头部/);assert.equal(f.L.lastGraspContact.ok,false);
  head.position.x=-.2;assert.equal(C.headContact(f.L).depth,0);
});
test('head gate prevents put release even with exact support and orientation',()=>{
  const f=fixture();touch(f);C.bindGrasp(f.L,f.item,'left');task(f,'put');
  f.L.graspHeadBoxes=()=>[{id:'shelf',center:[5,5],size:[.1,.1],y0:5,y1:5.3,soft:0}];
  C.finalizeGrasp(f.L,1.21);assert.equal(f.item.held,'left');assert.ok(f.item.graspBinding);
  assert.ok(f.L.lastGraspContact.headDepth>0);close(f.L.lastGraspContact.support,0);
});
test('direct-home and generic support heights use rotated extents, preserving flat-book orientation',()=>{
  const f=fixture(),q=new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1,0,0),Math.PI/2).toArray();
  const origin={position:[.2,.8,.3],quaternion:q};close(C.supportGap(f.item,origin,0),.775);
  const placed=C.poseOnSupport(f.item,[1,.7,2],q);
  samePose(placed,{position:[1,.725,2],quaternion:q});close(C.supportGap(f.item,placed,.7),0);
  close(f.item.half,.12);assert.notEqual(placed.position[1],.7+f.item.half);
  delete f.item.graspHalfExtents;assert.throws(()=>C.poseOnSupport(f.item,[1,.7,2],q),/支撑几何/);
});
