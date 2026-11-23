// Experimental pure geometry only. No runtime imports this module by default.
// Lengths: metres; quaternion: xyzw; angles named *Degrees: degrees, others radians.
import { ROM, twistAngle } from './rom.js';

const RAD = Math.PI / 180, TAU = Math.PI * 2, EPS = 1e-10;
const add = (a,b) => a.map((x,i)=>x+b[i]);
const sub = (a,b) => a.map((x,i)=>x-b[i]);
const mul = (a,k) => a.map(x=>x*k);
const dot = (a,b) => a.reduce((s,x,i)=>s+x*b[i],0);
const cross = (a,b) => [a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
const norm = a => Math.hypot(...a);
const clamp = (v,a,b) => Math.max(a,Math.min(b,v));
function vector(a,n,label) {
  if(!Array.isArray(a)||a.length!==n||!Array.from(a).every(Number.isFinite)) throw new TypeError(`${label}: expected ${n} finite numbers`);
  return a.slice();
}
function unit(a,n,label) { const v=vector(a,n,label),l=norm(v);if(l<=EPS)throw new RangeError(`${label}: zero vector`);return mul(v,1/l); }
const conjugate = q => [-q[0],-q[1],-q[2],q[3]];
function qmul(a,b) { const [x,y,z,w]=a,[u,v,t,s]=b;return[w*u+x*s+y*t-z*v,w*v-x*t+y*s+z*u,w*t+x*v-y*u+z*s,w*s-x*u-y*v-z*t]; }
function rotate(q,v) { const u=q.slice(0,3),c=cross(u,v);return add(v,mul(add(mul(c,q[3]),cross(u,c)),2)); }
const axisQ = (axis,a) => [...mul(axis,Math.sin(a/2)),Math.cos(a/2)];

/** Exact sphere-intersection elbow/knee point. An unreachable target is reported,
 * never silently clamped and relabelled as a successful IK solution. Pole is a
 * world point selecting the bend plane. Swivel rotates that plane about root→end. */
export function solveTwoBone({root,end,upperLength,lowerLength,pole,swivel=0}) {
  const A=vector(root,3,'root'),C=vector(end,3,'end'),P=vector(pole,3,'pole');
  const a=upperLength,b=lowerLength;
  if(!(Number.isFinite(a)&&a>0&&Number.isFinite(b)&&b>0&&Number.isFinite(swivel)))throw new RangeError('invalid lengths/swivel');
  const d=norm(sub(C,A)),minimum=Math.abs(a-b),maximum=a+b;
  if(d<=EPS)return{reachable:false,reason:'coincident-endpoints',distance:d,minimum,maximum,residual:minimum,joint:null};
  if(d<minimum-EPS||d>maximum+EPS)return{reachable:false,reason:'outside-reach-shell',distance:d,minimum,maximum,
    residual:Math.max(minimum-d,d-maximum,0),joint:null};
  const axis=mul(sub(C,A),1/d),x=(a*a-b*b+d*d)/(2*d),radius=Math.sqrt(Math.max(0,a*a-x*x));
  let bend=sub(sub(P,A),mul(axis,dot(sub(P,A),axis))),fallbackPole=false;
  if(norm(bend)<=EPS){
    const k=axis.map(Math.abs).indexOf(Math.min(...axis.map(Math.abs))),v=[0,0,0];v[k]=1;
    bend=cross(axis,v);fallbackPole=true;
  }
  bend=mul(bend,1/norm(bend));
  bend=add(mul(bend,Math.cos(swivel)),mul(cross(axis,bend),Math.sin(swivel)));
  const joint=add(add(A,mul(axis,x)),mul(bend,radius));
  return{reachable:true,reason:null,distance:d,minimum,maximum,residual:0,joint,end:C,
    circleCenter:add(A,mul(axis,x)),circleRadius:radius,axis,fallbackPole,
    elbowDegrees:Math.acos(clamp((d*d-a*a-b*b)/(2*a*b),-1,1))/RAD};
}

export function wristAngles(handRelativeQuaternion,side='left') {
  if(!['left','right'].includes(side))throw new TypeError('unknown hand side');
  const out=[side==='left'?1:-1,0,0],d=rotate(unit(handRelativeQuaternion,4,'handQuaternion'),out),ax=dot(d,out);
  return{flexDegrees:Math.atan2(-d[1],ax)/RAD,deviationDegrees:Math.atan2(d[2],ax)/RAD,axial:ax,direction:d};
}

/** Analytically enumerate feasible twist intervals from their wrist-limit boundaries.
 * Rotating about the normalized forearm's local ±X does not change the wrist point.
 * No wrist limit is weakened; no physical feasibility is claimed beyond these DOFs.
 * Inputs describe the already position-solved forearm, and desired hand WORLD rotation. */
export function solveForearmTwist({parentWorldQuaternion,forearmLocalQuaternion,handWorldQuaternion,side='left',limits=ROM}) {
  if(!['left','right'].includes(side))throw new TypeError('unknown hand side');
  const parent=unit(parentWorldQuaternion,4,'parentQuaternion'),local=unit(forearmLocalQuaternion,4,'forearmQuaternion');
  const hand=unit(handWorldQuaternion,4,'handQuaternion'),sign=side==='left'?1:-1,out=[sign,0,0];
  const initial=twistAngle(local,out),lo=(-limits.forearm.sup-initial)*RAD,hi=(limits.forearm.pron-initial)*RAD;
  if(![lo,hi,limits.wrist.flex,limits.wrist.ext,limits.wrist.ulnar,limits.wrist.radial].every(Number.isFinite)||lo>hi)
    throw new RangeError('invalid ROM limits');
  const rel=qmul(conjugate(qmul(parent,local)),hand),d=rotate(rel,out),ax=dot(d,out),r=Math.hypot(d[1],d[2]);
  const phase=Math.atan2(d[2],d[1]);
  const at=delta=>{
    const lower=unit(qmul(local,axisQ(out,delta)),4,'solvedForearm'),world=qmul(parent,lower);
    const wrist=unit(qmul(conjugate(world),hand),4,'solvedWrist'),angles=wristAngles(wrist,side);
    const twist=twistAngle(lower,out);
    const excess=Math.max(0,angles.flexDegrees-limits.wrist.flex,-limits.wrist.ext-angles.flexDegrees,
      angles.deviationDegrees-limits.wrist.radial,-limits.wrist.ulnar-angles.deviationDegrees,
      twist-limits.forearm.pron,-limits.forearm.sup-twist);
    return{delta,deltaDegrees:delta/RAD,forearmLocalQuaternion:lower,handLocalQuaternion:wrist,
      forearmTwistDegrees:twist,...angles,excessDegrees:excess,feasible:excess<=1e-7};
  };
  const boundaries=[lo,hi,clamp(0,lo,hi)];
  function includeTheta(theta){
    for(let k=-2;k<=2;k++){
      const delta=(phase-theta+k*TAU)/sign;
      if(delta>=lo-EPS&&delta<=hi+EPS)boundaries.push(clamp(delta,lo,hi));
    }
  }
  if(ax>EPS&&r>EPS){
    // y'=r*cos(theta), z'=r*sin(theta), theta=phase-sign*delta.
    for(const y of [-ax*Math.tan(limits.wrist.flex*RAD),ax*Math.tan(limits.wrist.ext*RAD)]){
      if(Math.abs(y)<=r){const a=Math.acos(clamp(y/r,-1,1));includeTheta(a);includeTheta(-a);}
    }
    for(const z of [-ax*Math.tan(limits.wrist.ulnar*RAD),ax*Math.tan(limits.wrist.radial*RAD)]){
      if(Math.abs(z)<=r){const a=Math.asin(clamp(z/r,-1,1));includeTheta(a);includeTheta(Math.PI-a);}
    }
  }
  boundaries.sort((a,b)=>a-b);
  const points=boundaries.filter((v,i)=>!i||Math.abs(v-boundaries[i-1])>EPS),intervals=[];
  for(let i=1;i<points.length;i++)if(at((points[i-1]+points[i])/2).feasible)intervals.push([points[i-1],points[i]]);
  const candidates=[...points,...intervals.map(([a,b])=>clamp(0,a,b))].map(at);
  // Feasibility dominates minimal twist; a best rejected candidate remains rejected.
  candidates.sort((a,b)=>Number(b.feasible)-Number(a.feasible)||a.excessDegrees-b.excessDegrees||Math.abs(a.delta)-Math.abs(b.delta));
  return{...candidates[0],initialTwistDegrees:initial,intervals,examined:candidates.length,
    reason:candidates[0].feasible?null:ax<=0?'wrist-points-backward':'no-ROM-feasible-twist'};
}

export const STATIC_GRASP_LIMITS = Object.freeze({palmPosition:.01,palmAngle:10*RAD,footPosition:.003,footAngle:2*RAD,
  romExcessDegrees:.05,collisionDepth:0,headDepth:0,balanceMargin:0,supportGap:.003});

/** Conjunctive hard constraints. Weighted costs rank rejected poses only; a low
 * cost can never compensate for collision, missing evidence or another violation.
 * Foot thresholds are explicit prototype acceptance rules, not relaxed runtime ROM. */
export function scoreStaticGrasp(metrics,{limits=STATIC_GRASP_LIMITS,requireSupport=false}={}) {
  const names=['palmPosition','palmAngle','footPosition','footAngle','romExcessDegrees','collisionDepth','headDepth','balanceMargin'];
  if(requireSupport)names.push('supportGap');
  const violations=[],missing=[],invalid=[];let cost=0,maxViolation=0;
  for(const name of names){
    const value=metrics?.[name],limit=limits?.[name];
    if(!Number.isFinite(value)||!Number.isFinite(limit)){missing.push(name);continue;}
    const signed=name==='balanceMargin'||name==='supportGap';
    if((!signed&&value<0)||(name!=='balanceMargin'&&limit<0)){invalid.push(name);continue;}
    const actual=name==='supportGap'?Math.abs(value):value;
    const excess=name==='balanceMargin'?Math.max(0,limit-actual):Math.max(0,actual-limit);
    if(excess>1e-9){
      const scale=Math.max(Math.abs(limit),name==='romExcessDegrees'?.05:.005),normalized=excess/scale;
      violations.push({name,value,limit,excess});cost+=normalized*normalized;maxViolation=Math.max(maxViolation,normalized);
    }
  }
  const complete=!missing.length&&!invalid.length;
  return{feasible:complete&&!violations.length,evidenceComplete:complete,missing,invalid,violations,
    violationCost:complete?cost:null,maxNormalizedViolation:complete?maxViolation:null};
}
