// Scoped stationary cup handoff. Continuous world elbow/hand references are captured
// at the accepted binding frame; all normal ROM/contact/filter layers still run after it.
import * as T from 'three';
const clamp=x=>Math.max(0,Math.min(1,x));
export const cupCurve=x=>{const t=clamp(x);return t*t*t*(10+t*(-15+6*t));};
export const cupControlWeight=phase=>1-cupCurve((phase-.75)/.25);
const finite=v=>v&&['x','y','z'].every(k=>Number.isFinite(v[k]));
export function cupElbow(shoulder,target,a,b,pole,hint,u=0){
  if(![shoulder,target,pole,hint].every(finite)||![a,b,u].every(Number.isFinite)||a<=0||b<=0||Math.min(a,b)<=.00101)return null;
  const axis=target.clone().sub(shoulder),distance=axis.length();if(distance<1e-8)return null;axis.multiplyScalar(1/distance);
  const project=v=>v.clone().addScaledVector(axis,-v.dot(axis));
  let p=project(pole),h=project(hint);if(p.lengthSq()<1e-10)p.copy(h);if(p.lengthSq()<1e-10)return null;p.normalize();
  if(h.lengthSq()>1e-10){h.normalize();const angle=Math.atan2(axis.dot(p.clone().cross(h)),p.dot(h));p.applyAxisAngle(axis,angle*clamp(u));}
  const d=Math.max(Math.abs(a-b)+1e-5,Math.min(distance,a+b-.002));
  const along=(a*a-b*b+d*d)/(2*d),height=Math.sqrt(Math.max(0,a*a-along*along));
  return {elbow:shoulder.clone().addScaledVector(axis,along).addScaledVector(p,height),
    wrist:shoulder.clone().addScaledVector(axis,d),reachError:Math.abs(distance-d)};
}
export function captureCupHandoff(L,side){
  const nodes=['UpperArm','LowerArm','Hand'].map(n=>L.nodes[side+n]);if(nodes.some(n=>!n))return null;
  const [s,e,w]=nodes.map(n=>L._world(n)),a=e.clone().sub(s),b=w.clone().sub(e),axis=w.clone().sub(s).normalize();
  if(a.length()<1e-5||b.length()<1e-5)return null;
  return {a:a.length(),b:b.length(),upper:a.normalize(),lower:b.normalize(),pole:e.clone().sub(s).addScaledVector(axis,-e.clone().sub(s).dot(axis)),
    q:nodes.map(n=>n.getWorldQuaternion(new T.Quaternion()))};
}
export function applyCupHandoff(L,side,ph,target,hint,want){
  const c=ph?.cupPose,phase=ph?.phase,weight=cupControlWeight(phase);
  if(!c||!Number.isFinite(phase)||weight<=0)return false;
  const nodes=['UpperArm','LowerArm','Hand'].map(n=>L.nodes[side+n]);if(nodes.some(n=>!n))return false;
  const shoulder=L._world(nodes[0]),solution=cupElbow(shoulder,target,c.a,c.b,c.pole,hint,ph.u);if(!solution)return false;
  const swing=(a,b,q)=>new T.Quaternion().setFromUnitVectors(a,b.normalize()).multiply(q);
  const upper=swing(c.upper,solution.elbow.clone().sub(shoulder),c.q[0]);
  const lower=swing(c.lower,solution.wrist.clone().sub(solution.elbow),c.q[1]);
  const palm=new T.Vector3(0,-1,0).applyQuaternion(c.q[2]);
  const finalHand=new T.Quaternion().setFromUnitVectors(palm,want.clone().normalize()).multiply(c.q[2]);
  const hand=c.q[2].clone().slerp(finalHand,ph.u),desired=[upper,lower,hand];
  // Blend the complete world orientations to the original carry during the final
  // quarter. At phase 1 this branch is exactly absent, including its derivatives.
  const world=nodes.map((n,i)=>n.getWorldQuaternion(new T.Quaternion()).slerp(desired[i],weight));
  nodes.forEach((n,i)=>{n.quaternion.copy(n.parent.getWorldQuaternion(new T.Quaternion()).invert().multiply(world[i]));n.updateWorldMatrix(false,true);});
  ph.control={weight,phase,reachError:solution.reachError,elbow:solution.elbow.toArray()};
  return true;
}
