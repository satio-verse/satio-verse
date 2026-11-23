import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const raw=readFileSync(new URL('../../desktop/web/room-liquid.js',import.meta.url),'utf8');
const source=raw.replace(/^import .*;\r?$/gm,'').replace(/^export /gm,'')+'\nglobalThis.geometry=pourRimPoint;';
const context=vm.createContext({THREE:{Vector3:class {}}});vm.runInContext(source,context);
const rim=(...args)=>{const p=context.geometry(...args);return p?Array.from(p):null;};
const distance=(a,b)=>Math.hypot(...a.map((v,i)=>v-b[i]));
const close=(a,b,eps=1e-10)=>assert.ok(distance(a,b)<eps,JSON.stringify({a,b}));
const qz=a=>[0,0,Math.sin(a/2),Math.cos(a/2)];
const rotateY=(p,a)=>[Math.cos(a)*p[0]+Math.sin(a)*p[2],p[1],-Math.sin(a)*p[0]+Math.cos(a)*p[2]];
const multiply=(a,b)=>[a[3]*b[0]+a[0]*b[3]+a[1]*b[2]-a[2]*b[1],a[3]*b[1]-a[0]*b[2]+a[1]*b[3]+a[2]*b[0],a[3]*b[2]+a[0]*b[1]-a[1]*b[0]+a[2]*b[3],a[3]*b[3]-a[0]*b[0]-a[1]*b[1]-a[2]*b[2]];

test('upright rim approaches from the mouth bearing, rather than the cup centre',()=>{
  close(rim([0,0,0],[0,0,0,1],.03,.06,[.2,.06,0]),[.03,.06,0]);
  close(rim([0,0,0],[0,0,0,1],.03,.06,[0,.1,-.2]),[0,.06,-.03]);
});
test('tilted cup uses the lowest geometric edge even if a different rim point is closer to mouth',()=>{
  const a=.7,q=qz(-a),expected=[.06*Math.sin(a)+.03*Math.cos(a),.06*Math.cos(a)-.03*Math.sin(a),0];
  close(rim([0,0,0],q,.03,.06,[.3,.2,0]),expected);
  close(rim([0,0,0],q,.03,.06,[-.3,.2,0]),expected);
});
test('near-upright limit is continuous, including opposite mouth and pouring bearings',()=>{
  const mouth=[-.3,.06,0],base=rim([0,0,0],qz(0),.03,.06,mouth);
  for(const angle of [1e-8,1e-7,1e-6])assert.ok(distance(base,rim([0,0,0],qz(-angle),.03,.06,mouth))<1e-6);
  let previous=base;
  for(let i=1;i<=800;i++){const p=rim([0,0,0],qz(-.08*i/800),.03,.06,mouth);assert.ok(p.every(Number.isFinite));assert.ok(distance(previous,p)<.0002);previous=p;}
});
test('translation and world-yaw rotations transform the geometric target consistently',()=>{
  const p=[.4,.8,-.2],q=qz(-.6),mouth=[.7,1,-.1],base=rim(p,q,.04,.09,mouth),delta=[2,-.3,4];
  close(rim(p.map((v,i)=>v+delta[i]),q,.04,.09,mouth.map((v,i)=>v+delta[i])),base.map((v,i)=>v+delta[i]));
  const a=1.2,qy=[0,Math.sin(a/2),0,Math.cos(a/2)];
  close(rim(rotateY(p,a),multiply(qy,q),.04,.09,rotateY(mouth,a)),rotateY(base,a));
});
test('quaternion sign and normalization do not change the rim target',()=>{
  const q=qz(-.9),args=[[0,0,0],q,.03,.08,[.2,.2,0]],base=rim(...args);
  close(rim(args[0],q.map(v=>-v),...args.slice(2)),base);
  close(rim(args[0],q.map(v=>v*3),...args.slice(2)),base);
});
test('recorded nearest-rim false contact is rejected by the geometric pouring edge',()=>{
  const p=[3.1348072797454596,1.2687401276296613,-2.6201625060005833];
  const q=[.3055540665473267,.07685096695535062,-.2587668017863684,.913110280079944];
  const mouth=[3.214159416222543,1.3191335010808294,-2.610188268309374];
  const actualOutlet=[3.179118054033164,1.2901932259795228,-2.5759643103661505];
  const point=rim(p,q,.026775000430643455,.06050008261203766,mouth);
  assert.ok(distance(point,actualOutlet)<.00001);
  assert.ok(distance(point,mouth)>.045); // nearest visible rim was only 30.018mm
});
test('invalid geometry or undefined upright bearing fails closed',()=>{
  assert.equal(rim([0,0,0],[0,0,0,0],.03,.06,[1,0,0]),null);
  assert.equal(rim([0,0,0],[0,0,0,1],0,.06,[1,0,0]),null);
  assert.equal(rim([0,0,0],[0,0,0,1],.03,.06,[0,.2,0]),null);
  assert.equal(rim([0,0,0],[0,0,0,1],.03,.06,[NaN,0,0]),null);
});
