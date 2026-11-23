// Non-rigid cup pickup preflight. Matrix/vector arithmetic only: no live pose writes,
// Object3D allocations, random draws, liquid truth or claims of finger-surface contact.
import * as T from 'three';
import {samplePose, qmul} from './mm.js';
import {toCell, toWorld, walkable, findPath} from './nav.js';
import {partsDepth} from './pain.js';
const UP = new T.Vector3(0, 1, 0);
export const CUP_BIND_TOLERANCE = 0.01;
export const cupPickItem = i => !!(i && !i.physical && !i.grasp && i.grip === 'cup'
  && i.size && ['x','y','z'].every(k => Number.isFinite(i.size[k]) && i.size[k] > 0));
export function cupPickError(code, message) { const e = new Error(message); e.code = 'CUP_PICK_' + code; return e; }
export function cupPutError(code, message) { const e = new Error(message); e.code = 'CUP_PUT_' + code; return e; }
// Exactly the existing Props cup convention. Proxy, inverse IK and final gate share it.
export function cupCenter(wrist, middle, normal, size) {
  if (![wrist,middle,normal,size].every(v=>v && ['x','y','z'].every(k=>Number.isFinite(v[k]))) || ['x','y','z'].some(k=>size[k]<=0)) return null;
  const n = normal.clone().setY(0), d = middle.clone().sub(wrist).normalize();
  if (n.lengthSq() < 0.04) n.copy(d).setY(0);
  if (n.lengthSq() < 1e-6) return null;
  return wrist.clone().lerp(middle, .55).addScaledVector(n.normalize(), Math.min(size.x,size.z)/2+.006)
    .add(new T.Vector3(0,-size.y*.08,0));
}
export function liveCupCenter(L, item, side) {
  const hand = L.nodes[side+'Hand'], mid = L.vrm.humanoid.getNormalizedBoneNode(side+'MiddleProximal');
  if (!hand || !mid) return null;
  return cupCenter(L._world(hand),L._world(mid),new T.Vector3(0,-1,0).applyQuaternion(hand.getWorldQuaternion(new T.Quaternion())),item.size);
}
// Retargeted event FK uses the same world rotations/alignment and rest offsets as _write.
// It predicts reach, not post-filter execution. The final displayed-pose gate is mandatory.
export function clipGeometry(L, g, side, frame = null) {
  const pose=[], c=L.acts.clips[g.clip], f=frame ?? c.start+g.eventOffset, sample=samplePose(L.acts,f,pose), q={}, p={};
  L.db.bones.forEach((n,i)=>{ if(L.nodes[n]) q[n]=new T.Quaternion(...qmul(pose[i],L.align[n])); });
  const position=n=>{
    if(p[n])return p[n]; const parent=L.parentOf[n];
    if(n==='hips')return p[n]=new T.Vector3(sample.hips[0]*L.hipHeight,sample.hips[1]*L.hipHeight,sample.hips[2]*L.hipHeight);
    if(!parent||!q[parent]||!L.rest[n]||!L.rest[parent])return null;
    const pp=position(parent); return pp ? p[n]=L.rest[n].clone().sub(L.rest[parent]).applyQuaternion(q[parent]).add(pp) : null;
  };
  for(const n of Object.keys(L.nodes))position(n);
  const hand=p[side+'Hand'], end=L.rest[side+'HandEnd'];
  if(!hand||!end||!q[side+'Hand'])return null;
  const middle=end.clone().sub(L.rest[side+'Hand']).applyQuaternion(q[side+'Hand']).add(hand);
  return {p,q,middle,hand,shoulder:p[side+'UpperArm'],elbow:p[side+'LowerArm']};
}
const body=[['head',.1],['chest',.11],['hips',.11],['leftLowerLeg',.06],['rightLowerLeg',.06]];
export function cupBodyDepth(L, groups, points) {
  const scale=L.hipHeight/.78;let depth=0;
  for(const[n,r]of body){const p=points[n];if(p)depth=Math.max(depth,partsDepth(groups,p.toArray(),r*scale)-.015);}
  return Math.max(0,depth);
}
function atGeometry(geom,pos,yaw,L) {
  const rot=new T.Quaternion().setFromAxisAngle(UP,yaw),offset=new T.Vector3(pos[0],0,pos[1]),p={};
  for(const[n,v]of Object.entries(geom.p))p[n]=v.clone().applyQuaternion(rot).add(offset);
  if(p.head)p.head.add(new T.Vector3(0,.09*L.hipHeight/.78,0).applyQuaternion(geom.q.head).applyQuaternion(rot));
  return {p,rot,offset};
}
export function planCupPick(L,item,side,groups,center = null) {
  const begun=performance.now(),target=center ? center.clone() : item.mesh.position.clone(),grid=L.grid,boxes=L.avatar.collision?.()||L._standBoxes();
  const nodes=['UpperArm','LowerArm','Hand'].map(n=>L.nodes[side+n]);
  if(nodes.some(n=>!n))return null;
  const arm=L._world(nodes[0]).distanceTo(L._world(nodes[1]))+L._world(nodes[1]).distanceTo(L._world(nodes[2]));
  if (![target.x,target.y,target.z,arm,L.hipHeight,L.acts.fps,grid.cell].every(Number.isFinite) || arm<=0 || L.hipHeight<=0 || L.acts.fps<=0 || grid.cell<=0) return null;
  const [ci,cj]=toCell(grid,target.x,target.z),radius=Math.ceil((arm+L.hipHeight*.65)/grid.cell);
  let considered=0,feasible=0,best=null;
  for(const g of groups){
    const clip=L.acts.clips[g.clip],candidates=[];
    if(!clip || ![clip.start,clip.end,g.eventOffset].every(Number.isFinite) || clip.start<0 || clip.end<=clip.start || g.eventOffset<0 || g.eventOffset>=clip.end-clip.start || L._clipStrain(g)>0)continue;
    const geom=clipGeometry(L,g,side);if(!geom?.shoulder)continue;
    for(let j=cj-radius;j<=cj+radius;j++)for(let i=ci-radius;i<=ci+radius;i++){
      if(!walkable(grid,i,j))continue;const pos=toWorld(grid,i,j),face=Math.atan2(target.x-pos[0],target.z-pos[1]);
      for(const turn of [0,-Math.PI/8,Math.PI/8]){
        considered++; const yaw=face+turn,{p,rot,offset}=atGeometry(geom,pos,yaw,L);
        const middle=geom.middle.clone().applyQuaternion(rot).add(offset),normal=new T.Vector3(0,-1,0).applyQuaternion(geom.q[side+'Hand']).applyQuaternion(rot);
        const center=cupCenter(p[side+'Hand'],middle,normal,item.size);if(!center)continue;
        const wrist=p[side+'Hand'].clone().add(target.clone().sub(center)),reach=p[side+'UpperArm'].distanceTo(wrist);
        if(reach>arm*.97||reach<arm*.25||cupBodyDepth(L,boxes,p)>1e-6)continue;
        const cost=Math.hypot(pos[0]-L.char.x,pos[1]-L.char.z)+Math.abs(reach-arm*.8)*2+Math.abs(turn)*.05;
        candidates.push({g,pos,yaw,cost,reach,arm});
      }
    }
    feasible+=candidates.length;
    candidates.sort((a,b)=>a.cost-b.cost);
    for(const c of candidates){
      if(!findPath(grid,[L.char.x,L.char.z],c.pos))continue;
      let safe=true;
      // Start/mid/end samples are preflight, not a swept/dynamic safety certificate.
      for(const f of [clip.start,clip.start+g.eventOffset/2,clip.end-1]){
        const sample=clipGeometry(L,g,side,f);
        if(!sample||cupBodyDepth(L,boxes,atGeometry(sample,c.pos,c.yaw,L).p)>1e-6){safe=false;break;}
      }
      if(safe){best=c;break;}
    }
    // Only a fully screened candidate stops the shuffled existing-clip search.
    if(best)break;
  }
  L.lastCupPickPlan={ms:performance.now()-begun,considered,feasible,
    chosen:best?{clip:best.g.clip,pos:best.pos.slice(),yaw:best.yaw,reach:best.reach,arm}:null,
    boundary:'retargeted FK preflight; final displayed binding-center gate required'};
  return best;
}
