import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {register} from 'node:module';
import {coffeeDecision} from '../../desktop/web/room-coffee-policy.js';

const THREE_URL=new URL('../../web/vendor/three/build/three.module.js',import.meta.url).href;
const STATIC_URL=new URL('../../web/',import.meta.url).href,PET_URL=new URL('../../desktop/web/',import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n){
  if(s==='three')return {url:${JSON.stringify(THREE_URL)},shortCircuit:true};
  if(s.startsWith('/static/'))return {url:new URL(s.slice(8),${JSON.stringify(STATIC_URL)}).href,shortCircuit:true};
  if(s.startsWith('/pet/'))return {url:new URL(s.slice(5),${JSON.stringify(PET_URL)}).href,shortCircuit:true};return n(s,c);}`)}`);
globalThis.location={search:''};
const T=await import('three');
const source=await readFile(new URL('../../desktop/web/room-acts.js',import.meta.url),'utf8');
const {SCRIPTS,Life}=await import(`data:text/javascript;base64,${Buffer.from(source+'\nexport {SCRIPTS};').toString('base64')}`);
const view=(level,extra={})=>({visible:true,level,known:false,category:'unknown',inferred:false,...extra});

test('coffee policy reads only the coarse observation, never hidden volume or temperature',()=>{
  for(const level of ['low','half','full']){
    const o=view(level);Object.defineProperties(o,{ml:{get(){throw Error('hidden ml');}},temp:{get(){throw Error('hidden temp');}}});
    assert.equal(coffeeDecision(o,{beans:0}).action,'reuse');
  }
  assert.equal(coffeeDecision(view('empty'),{beans:1}).action,'brew');
  assert.equal(coffeeDecision(view('empty'),{beans:0}).action,'refuse');
  assert.equal(coffeeDecision(view('unknown'),{beans:5}).action,'observe');
  assert.equal(coffeeDecision({visible:false,level:'empty'},{beans:5}).action,'observe');
});
test('recent measured hot defers drinking; brewing inference is not a measured current temperature',()=>{
  assert.equal(coffeeDecision(view('half',{known:true,category:'hot'}),{beans:9}).action,'wait');
  assert.equal(coffeeDecision(view('half',{known:true,category:'hot',inferred:true}),{beans:0}).action,'reuse');
  assert.equal(coffeeDecision(view('half',{known:false,category:'unknown'}),{beans:0}).action,'reuse');
});

function fixture(observation=view('half'),beans=0){
  const calls=[],cup=new T.Object3D(),button=new T.Object3D();cup.position.set(1,1,1);
  const state={observation};
  const r={scene:{coffee:{stand:[1,2],cups:['cup'],power:'power',brew:'brew'}},sightMap:{FAVORITE:'cup',NAMES:{cup:'杯子'}},
    needs:{actualConsumption:true,stock:{beans},v:{energy:40,water:20},effectiveEnergy:40,use(){assert.fail('no scripted stock charge');}},
    liquid:{inspect:()=>state.observation,beginPreparation:(stock,id)=>{calls.push(['begin',stock,id]);return 'prep';},
      endPreparation:()=>{calls.push(['end']);return {ok:true,preparedMl:60};}},
    props:{node:id=>id==='cup'?cup:button,asItem(){},stream:p=>calls.push(['stream',!!p]),wake(){},settle(){}},
    layer:{items:{cup:{id:'cup',mesh:cup,half:.04,held:null}},targets:{},lookAt(){}},v:{setEmotion(){}}};
  const ctx={r,life:{actionDetail:null},say(){},walk:async()=>calls.push(['walk']),wait:async s=>calls.push(['wait',s]),
    reach:async(side,p,cb)=>{calls.push(['reach']);cb?.();},run:async step=>{calls.push(['run',step.do]);throw Error('STOP_BEFORE_PHYSICAL_PICK');},later(){}};
  return {r,ctx,calls,state};
}
test('a second request with an existing cup and no beans reaches pick without preparing or charging',async()=>{
  const f=fixture(view('empty'),1);
  await assert.rejects(()=>SCRIPTS.coffee(f.ctx,{}),/STOP_BEFORE_PHYSICAL_PICK/);
  assert.equal(f.calls.filter(c=>c[0]==='begin').length,1);
  f.state.observation=view('half');f.r.needs.stock.beans=0;const first=f.calls.length;
  await assert.rejects(()=>SCRIPTS.coffee(f.ctx,{}),/STOP_BEFORE_PHYSICAL_PICK/);
  assert.equal(f.calls.slice(first).some(c=>c[0]==='begin'||c[0]==='stream'||c[0]==='reach'),false);
  assert.ok(f.calls.slice(first).some(c=>c[0]==='run'&&c[1]==='pick'));
});
test('known hot, unknown level and empty-without-beans fail before machine or pick and never claim consumption',async()=>{
  for(const [o,beans,action] of [[view('half',{known:true,category:'hot'}),5,'wait'],[view('unknown'),5,'observe'],[view('empty'),0,'refuse']]){
    const f=fixture(o,beans);
    await assert.rejects(()=>SCRIPTS.coffee(f.ctx,{}));
    assert.equal(f.ctx.life.actionDetail.action,action);assert.equal(f.ctx.life.actionDetail.consumed,false);
    assert.equal(f.calls.some(c=>['begin','stream','reach','run'].includes(c[0])),false);
  }
});
test('visible low remainder is reused without topping up; wrist/drink guards remain responsible for reachability',async()=>{
  const f=fixture(view('low'),8);await assert.rejects(()=>SCRIPTS.coffee(f.ctx,{}),/STOP_BEFORE_PHYSICAL_PICK/);
  assert.equal(f.calls.some(c=>c[0]==='begin'||c[0]==='stream'),false);
});
test('explicit request can inspect without beans, automatic choice requires a currently visible reusable drink',()=>{
  const f=fixture(view('unknown'),0),coffee={id:'coffee',script:'coffee',need:'beans'};
  const life=Object.create(Life.prototype);Object.assign(life,{r:f.r,requests:{blocked:false},base:[coffee],order:[],current:{},busyWith:new Map()});
  assert.equal(life.canRequest(coffee),true);assert.equal(life.can(coffee,false),false);
  assert.equal(life.goDo('coffee'),true);
  f.state.observation=view('half');assert.equal(life.can(coffee,false),true);
  f.state.observation={visible:false,level:'unknown',known:false,remembered:{level:'half'},retry:{state:'due'}};
  assert.equal(life.can(coffee,false),true);
  f.state.observation.retry.state='wait';assert.equal(life.can(coffee,false),false);
  f.state.observation={visible:false,level:'unknown',remembered:{level:'empty'},retry:{state:'none'}};
  assert.equal(life.can(coffee,false),false);
  f.state.observation=view('half',{known:true,category:'hot'});assert.equal(life.can(coffee,false),false);
  const snack={script:'snack',need:'snacks'};assert.equal(life.canRequest(snack),false);
  assert.equal(life.canRequest({script:'afford',ctx:null}),false);
  assert.equal(life.canRequest({script:'afford',ctx:{},need:'snacks'}),false);
  f.r.needs.stock.snacks=1;assert.equal(life.canRequest({script:'afford',ctx:{},need:'snacks'}),true);
  life.recoveryError='cleanup failed';assert.equal(life.canRequest(coffee),false);assert.equal(life.can(coffee,false),false);
});
