import {test} from 'node:test';
import assert from 'node:assert/strict';
import {register} from 'node:module';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {sipExitWeight} from '../../desktop/web/room-sip-exit.js';
const THREE_URL=new URL('../../web/vendor/three/build/three.module.js',import.meta.url).href;
const STATIC_URL=new URL('../../web/',import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n){
 if(s==='three')return {url:${JSON.stringify(THREE_URL)},shortCircuit:true};
 if(s.startsWith('/static/'))return {url:new URL(s.slice(8),${JSON.stringify(STATIC_URL)}).href,shortCircuit:true};return n(s,c);}`)}`);
globalThis.location={search:''};
const T=await import('three');
const {PoseLayer}=await import('../../desktop/web/room-poses.js');
function fixture(side='right',exact=true){
 const scene=new T.Group(),nodes={};
 for(const n of ['hips','neck','head',...['left','right'].flatMap(s=>[s+'UpperArm',s+'LowerArm',s+'Hand'])]){nodes[n]=new T.Bone();scene.add(nodes[n]);}
 const calls=[],layer={items:{cup:{held:side,grip:'cup'}},vrm:{scene,humanoid:{getNormalizedBoneNode:n=>nodes[n]}},char:{x:0,z:0,yaw:0},hipHeight:.78,_twoBone:(...a)=>calls.push(a)};
 const p=new PoseLayer(layer);p._palm=()=>{};p.set('sip',{side,sipExact:exact,sipPalmIn:.5});p.W=1;
 for(const n of [side+'UpperArm',side+'LowerArm',side+'Hand','neck']){p.wb[n]=1;nodes[n].quaternion.setFromAxisAngle(new T.Vector3(1,0,0),.6);p.q[n]=new T.Quaternion();}
 return {p,layer,nodes,calls};
}
test('minimum jerk withdrawal is finite, monotonic and has zero endpoint velocity/acceleration',()=>{
 assert.equal(sipExitWeight(-1),1);assert.equal(sipExitWeight(0),1);assert.equal(sipExitWeight(3),0);assert.equal(sipExitWeight(8),0);
 let prev=1;for(let i=0;i<=300;i++){const w=sipExitWeight(i/100);assert.ok(w<=prev+1e-12&&w>=0);prev=w;}
 const h=1e-3;for(const t of [0,3]){assert.ok(Math.abs((sipExitWeight(t+h)-sipExitWeight(t-h))/(2*h))<1e-5);assert.ok(Math.abs((sipExitWeight(t+h)-2*sipExitWeight(t)+sipExitWeight(t-h))/(h*h))<.001);}
 for(const x of [NaN,Infinity])assert.throws(()=>sipExitWeight(x));assert.throws(()=>sipExitWeight(0,0));
});
for(const side of ['left','right'])test('actual '+side+' sip retains sole exit ownership and displayed starting pose',()=>{
 const {p,layer,nodes,calls}=fixture(side);const before=nodes[side+'Hand'].quaternion.clone();
 p.set(null);const exit=p._sipExit;p.update(0);assert.ok(nodes[side+'Hand'].quaternion.angleTo(before)<1e-12);assert.equal(p.opts.side,side);assert.equal(p.opts.sipExact,true);
 assert.equal(layer.carryYield[side],true);assert.equal(p.cw[side],0);assert.equal(calls.length,0);assert.equal(p.moving,true);
 p.set(null);assert.equal(p._sipExit,exit);
 for(let i=0;i<90;i++){for(const b of Object.values(nodes))b.quaternion.identity();p.update(1/30);}
 // Floating addition may leave less than a picosecond; the next actual frame finishes.
 p.update(1/30);assert.equal(p.exitingSip,false);assert.equal(p.W,0);assert.deepEqual(p.opts,{});assert.equal(layer.carryYield[side],false);
});
test('legacy sip and non-held exact sip keep the existing exponential exit',()=>{
 for(const noCup of [false,true]){const {p,layer}=fixture('right',noCup);if(noCup)layer.items={};p.set(null);assert.equal(p.exitingSip,false);assert.deepEqual(p.opts,{});p._main(.1);assert.ok(p.W>0&&p.W<1);}
});
test('a new explicit pose supersedes exit without a delayed old pose restoration',()=>{
 const {p}=fixture();p.set(null);p.update(.3);p.set('fallen',{});assert.equal(p.exitingSip,false);assert.equal(p.name,'fallen');assert.deepEqual(p.opts,{});
});
const source=readFileSync(new URL('../../desktop/web/room-acts.js',import.meta.url),'utf8');
const settleSource=source.slice(source.indexOf('async function settleSip('),source.indexOf('/** 喝 (sip)'));
async function settleCase(stall=false,lateJump=false){
 let time=0,exitAt=Infinity,done=false,error=null,checks=[];const queue=[];
 const node={getWorldQuaternion:q=>q.identity(),getWorldPosition:p=>p.set(lateJump&&time-exitAt<3400&&time-exitAt>=3000?(time%80? .02:0):0,0,0)};
 const poses={name:'sip',W:1,exitingSip:false,set(n){this.name=n;this.exitingSip=true;exitAt=time;}};
 const ctx={r:{props:{tilt(){},node:()=>node},liquid:{tiltNow:{cup:0}},poses}};
 const settle=vm.runInNewContext('('+settleSource+')',{THREE:T,performance:{now:()=>time},setTimeout:(fn,ms)=>queue.push({fn,at:time+ms})});
 settle(ctx,'cup').then(()=>done=true,e=>{error=e;done=true;});await Promise.resolve();
 while(!done&&queue.length&&time<20000){queue.sort((a,b)=>a.at-b.at);const task=queue.shift();time=task.at;
  if(!stall&&time-exitAt>=3000){poses.exitingSip=false;poses.W=0;}
  checks.push(time);task.fn();await Promise.resolve();await Promise.resolve();}
 return {time,exitAt,error,done,checks};
}
test('settle waits for actual exit completion and stable final carry, including a late handoff jump',async()=>{
 const a=await settleCase(false,true);assert.equal(a.error,null);assert.equal(a.done,true);assert.ok(a.time-a.exitAt>=3800);
});
test('a stalled exit fails within its finite deadline instead of permitting put',async()=>{
 const a=await settleCase(true);assert.equal(a.done,true);assert.match(a.error.message,/退杯交接/);assert.ok(a.time-a.exitAt>=7500&&a.time-a.exitAt<7600);
});
