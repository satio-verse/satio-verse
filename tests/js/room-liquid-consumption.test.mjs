import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { ConsumptionOutbox, sipTarget } from '../../desktop/web/room-consumption.js';
import { capacityMl, heatEffect } from '../../web/physics/liquid.js';
import { COFFEE_EVIDENCE, restoreCoffeeEvidence, tastedCoffee, coffeeThermalView } from '../../desktop/web/room-coffee-policy.js';

const text = readFileSync(new URL('../../desktop/web/room-liquid.js', import.meta.url), 'utf8');
const source = text.replace(/^import .*;\r?$/gm, '').replace(/^export /gm, '') + '\nglobalThis.RoomLiquid = RoomLiquid;';
class Vector3 { constructor(x = 0, y = 0, z = 0) { Object.assign(this, { x, y, z }); } }
class Quaternion { constructor(x = 0, y = 0, z = 0, w = 1) { Object.assign(this, { x, y, z, w }); } }
class Box3 { setFromObject(o) { return Object.assign(this, o.box); } }
const ctx = vm.createContext({ THREE: { Vector3, Quaternion, Box3 }, ConsumptionOutbox, sipTarget, capacityMl, heatEffect,
  COFFEE_EVIDENCE, restoreCoffeeEvidence, tastedCoffee, coffeeThermalView,
  crypto, Date, performance, console, PART_NAMES: {} }); vm.runInContext(source, ctx);
function make() {
  let saved = null, fail = false, visible = true; const effects = [], cups = { cup: { R: .03, H: .08, ml: 20, temp: 22, kind: 'coffee', capacity: 200 } };
  const liquid = Object.create(ctx.RoomLiquid.prototype);
  Object.assign(liquid, { world: { cups, fill: (id, ml) => (cups[id].ml += ml, ml), toJSON: () => ({ cups }), pour: () => {} },
    consumption: new ConsumptionOutbox(null, () => 'stream'), thermalEvidence: {}, sipping: {}, cups: {}, dirty: false,
    r: { scene: { sceneId: 'v8' }, layer: { items: {} }, body: { stimulus: (e) => effects.push(e) },
      props: { node: () => ({ visible }), flow: { visible: true, box: { min: { x: -.01, y: .1, z: -.01 }, max: { x: .01, y: .2, z: .01 } } } },
      sight: { now: () => ({ hand: visible ? ['cup'] : [], seen: {} }) },
      needs: { actualConsumption: true, canFeed: () => true, stock: { beans: 1 }, prepareStock: () => ({ ok: true }), consumeLiquid: () => ({ ok: true }) } },
    storage: { setItem: (key, data) => { if (fail) throw Error('storage'); saved = JSON.parse(data); } } });
  liquid.perceive = () => ({ cups: { cup: 'low' } });
  return { liquid, effects, hide: () => { visible = false; }, fail: (v) => { fail = v; }, saved: () => saved };
}
test('real RoomLiquid drink handler queues only measured drink events, including same-object duplicate protection', () => {
  const { liquid, effects } = make(); const e = { type: 'drink', cup: 'cup', ml: 2, temp: 70 };
  liquid._event(e); liquid._event(e);
  assert.equal(liquid.consumption.state.pending.length, 1); assert.equal(effects.length, 1);
  assert.equal(liquid.consumption.state.pending[0].ml, 2);
  liquid._event({ type: 'contact', part: 'rightHand', ml: 2, temp: 70, effect: heatEffect(70, 2) });
  assert.equal(liquid.consumption.state.pending.length, 1);
});
test('inspect only returns observed coarse state, not exact truth; unseen or stale cup temperature stays unknown', () => {
  const { liquid, hide } = make(); assert.equal(liquid.inspect('cup').temperature, 'unknown');
  liquid._event({ type: 'drink', cup: 'cup', ml: 1, temp: 9 }); const view = liquid.inspect('cup');
  assert.equal(view.temperature, 'cold'); assert.equal(view.known, true); assert.equal(view.category, 'cold'); assert.equal(view.text, '刚才喝到的是冷的'); assert.equal(view.inferred, false); assert.equal('ml' in view, false); assert.equal('temp' in view, false);
  liquid.thermalEvidence.cup.at -= 31000; assert.equal(liquid.inspect('cup').temperature, 'unknown');
  hide(); assert.equal(liquid.inspect('cup').visible, false);
});
test('RoomLiquid saves and restores hot caution separately from actual quantities and keeps unseen level memory', () => {
  const {liquid:l,saved}=make();l._event({type:'drink',cup:'cup',ml:.02,temp:69});
  l.inspect('cup');assert.equal(l.save(),true);
  const data=saved();assert.equal(data.coffeeEvidence.version,1);assert.equal(data.coffeeEvidence.thermal.cup.temperature,'hot');
  assert.equal('temp' in data.coffeeEvidence.thermal.cup,false);assert.equal('ml' in data.coffeeEvidence.levels.cup,false);
  const fresh=make();fresh.liquid._restoreCoffeeEvidence(data.coffeeEvidence);fresh.hide();
  const o=fresh.liquid.inspect('cup');assert.equal(o.visible,false);assert.equal(o.known,false);
  assert.equal(o.pastHot,true);assert.equal(o.retry.state,'wait');assert.equal(o.remembered.level,'low');
});
test('a current observed empty cup replaces old nonempty memory; unknown observation does not invent empty', () => {
  const {liquid:l,hide}=make();l.inspect('cup');assert.equal(l.cupEvidence.cup.level,'low');
  l.perceive=()=>({cups:{cup:'unknown'}});l.inspect('cup');assert.equal(l.cupEvidence.cup.level,'low');
  l.perceive=()=>({cups:{cup:'empty'}});l.inspect('cup');hide();assert.equal(l.inspect('cup').remembered.level,'empty');
});
test('running RoomLiquid detects wall-clock rollback and persists a bounded fresh caution without claiming warmth', () => {
  const {liquid:l,saved}=make();l._event({type:'drink',cup:'cup',ml:.02,temp:69});l.inspect('cup');
  l.evidenceClock=Date.now()+60000;
  const o=l.inspect('cup');assert.equal(o.known,false);assert.equal(o.pastHot,true);assert.equal(o.retry.state,'wait');
  assert.equal(l.thermalEvidence.cup.uncertain,true);assert.ok(l.thermalEvidence.cup.retryAt-Date.now()<=300000);
  assert.equal(l.save(),true);assert.equal(saved().coffeeEvidence.thermal.cup.uncertain,true);
});
test('a real new taste that first detects rollback is written to the repaired evidence object', () => {
  const {liquid:l}=make();l._event({type:'drink',cup:'cup',ml:.02,temp:69});
  const previous=l.thermalEvidence;l.evidenceClock=Date.now()+60000;
  l._event({type:'drink',cup:'cup',ml:.02,temp:55});
  assert.notEqual(l.thermalEvidence,previous);
  assert.equal(l.thermalEvidence.cup.temperature,'warm');assert.equal(l.inspect('cup').known,true);
  assert.equal(l.inspect('cup').pastHot,false);
});
test('begin/end alone creates no stock receipt; actual cup fill does, one preparation ID for the whole brew', () => {
  const { liquid, saved } = make(); liquid.world.cups.cup.ml=0;liquid.perceive=()=>({cups:{cup:'empty'}});
  const id = liquid.beginPreparation('beans','cup');
  assert.equal(liquid.endPreparation(id).ok, false); assert.equal(saved().consumption.preparations.length, 0);
  liquid.cups.cup = { bottom: { clone: () => ({}) } };
  liquid.r.props.node = () => ({ visible: true, localToWorld: () => ({ x: 0, y: 0, z: 0 }) });
  const seen = []; liquid.r.needs.prepareStock = (e) => (seen.push(e), { ok: true });
  const next = liquid.beginPreparation('beans','cup'); liquid._brew(.1); liquid._brew(.1);
  assert.equal(seen.length, 1); assert.equal(seen[0].id, next); assert.equal(seen[0].preparedMl, 1);
  const result = liquid.endPreparation(next); assert.equal(result.ok, true); assert.equal(result.preparedMl, 2);
});

test('brew refuses unobserved or nonempty cups and first-fill execution rejects a stale empty observation',()=>{
  const {liquid:l}=make();assert.throws(()=>l.beginPreparation('beans','cup'),/没有观察到空杯/);
  l.perceive=()=>({cups:{cup:'empty'}});l.world.cups.cup.ml=0;
  const id=l.beginPreparation('beans','cup');let stopped=false;
  l.r.props.stream=()=>{stopped=true;};l.r.props.node=()=>({visible:true,localToWorld:()=>({x:0,y:0,z:0})});
  l.cups.cup={bottom:{clone:()=>({})}};l.world.cups.cup.ml=10;
  l._brew(.1);assert.equal(l.world.cups.cup.ml,10);assert.equal(stopped,true);
  assert.equal(l.consumption.state.preparations.length,0);assert.equal(l.endPreparation(id).ok,false);
});

test('preparation is bound to the observed cup, never another empty cup under the stream',()=>{
  const {liquid:l}=make();l.world.cups.cup.ml=0;l.perceive=()=>({cups:{cup:'empty'}});
  l.world.cups.other={...l.world.cups.cup};l.cups.other={bottom:{clone:()=>({})}};
  l.r.props.node=()=>({visible:true,localToWorld:()=>({x:0,y:0,z:0})});l.r.props.stream=()=>{};
  const id=l.beginPreparation('beans','cup');l._brew(.1);
  assert.equal(l.world.cups.other.ml,0);assert.equal(l.consumption.state.preparations.length,0);
  assert.equal(l.endPreparation(id).ok,false);
});

test('actual observation preserves visible residue and uncertainty while legacy level bins remain unchanged',()=>{
  for(const [ml,level] of [[0,'empty'],[.2,'unknown'],[.5,'low'],[2,'low'],[20,'low'],[100,'half'],[180,'full']]){
    assert.equal(ctx.observedCupLevel({ml,capacity:200},true),level);
  }
  assert.equal(ctx.observedCupLevel({ml:2,capacity:200},false),'empty');
});
test('world save failure prevents settlement and further uncommitted brewing', () => {
  const { liquid, fail } = make(); let applied = 0; liquid.r.needs.consumeLiquid = () => (applied++, { ok: true });
  liquid._event({ type: 'drink', cup: 'cup', ml: 1, temp: 22 }); fail(true);
  assert.equal(liquid.save(), false); assert.equal(applied, 0); assert.equal(liquid.dirty, true);
  fail(false); assert.equal(liquid.save(), true); assert.equal(applied, 1); assert.equal(liquid.consumption.state.pending.length, 0);
});

test('inspect stable public shape distinguishes inferred brewing from actual taste and unknown', () => {
  const { liquid, hide } = make();
  for (const key of ['known', 'category', 'text', 'inferred', 'temperature', 'reason']) assert.ok(key in liquid.inspect('cup'));
  liquid.thermalEvidence.cup = { at: Date.now(), temperature: 'hot', inferred: true, reason: '刚冲好的咖啡，可能烫' };
  const inferred = liquid.inspect('cup'); assert.equal(inferred.known, true); assert.equal(inferred.inferred, true); assert.match(inferred.text, /可能/);
  liquid.thermalEvidence.cup.at -= 61000; assert.equal(liquid.inspect('cup').known, false);
  hide(); const unknown = liquid.inspect('cup'); assert.equal(unknown.known, false); assert.equal(unknown.category, 'unknown'); assert.equal(typeof unknown.text, 'string');
});

function tiltCase(actual = true) {
  const { liquid } = make();
  liquid.r.needs.actualConsumption = actual;
  liquid.r.poses = { W: 1, name: 'sip' };
  Object.assign(liquid, { sipping: { cup: { got: 0, spilled: 0 } }, lipGap: { cup: .07 },
    tiltGoal: { cup: .9 }, tiltNow: { cup: 0 }, tiltRaw: () => {}, _sipAngle: () => .9 });
  return liquid;
}
test('actual sip remains upright for distant or unknown lip contact, even beyond old timeout', () => {
  const l = tiltCase();
  for (let i = 0; i < 100; i++) l._tiltStep(.1);
  assert.equal(l.tiltNow.cup, 0);
  delete l.lipGap.cup;
  for (let i = 0; i < 30; i++) l._tiltStep(.1);
  assert.equal(l.tiltNow.cup, 0);
  l.lipGap.cup = .035; l._tiltStep(.1); assert.equal(l.tiltNow.cup, 0);
});
test('actual sip immediately aims upright when contact is lost or sip pose exits', () => {
  const l = tiltCase(); l.lipGap.cup = .03;
  for (let i = 0; i < 10; i++) l._tiltStep(.1);
  assert.ok(l.tiltNow.cup > .8);
  l.lipGap.cup = .046; l._tiltStep(.1); assert.ok(l.tiltNow.cup < .3);
  for (let i = 0; i < 10; i++) l._tiltStep(.1);
  assert.equal(l.tiltNow.cup, 0);
  l.lipGap.cup = .03; l._tiltStep(.1); assert.ok(l.tiltNow.cup > 0);
  l.r.poses.name = null;
  for (let i = 0; i < 10; i++) l._tiltStep(.1);
  assert.equal(l.tiltNow.cup, 0);
});
test('actual sip contact has 35/45mm hysteresis and requires close reacquisition after loss', () => {
  const l = tiltCase(); l.lipGap.cup = .04; l._tiltStep(.1); assert.equal(l.tiltNow.cup, 0);
  l.lipGap.cup = .034; l._tiltStep(.1); const started = l.tiltNow.cup; assert.ok(started > 0);
  l.lipGap.cup = .04; l._tiltStep(.1); assert.ok(l.tiltNow.cup > started);
  l.lipGap.cup = .046;
  for (let i = 0; i < 10; i++) l._tiltStep(.1);
  l.lipGap.cup = .04; l._tiltStep(.1); assert.equal(l.tiltNow.cup, 0);
  l.lipGap.cup = .034; l._tiltStep(.1); assert.ok(l.tiltNow.cup > 0);
});
test('legacy non-actual sip retains the original two-second distant-contact fallback', () => {
  const l = tiltCase(false);
  for (let i = 0; i < 10; i++) l._tiltStep(.1);
  assert.equal(l.tiltNow.cup, 0);
  for (let i = 0; i < 15; i++) l._tiltStep(.1);
  assert.ok(l.tiltNow.cup > 0);
});

function alignmentCase() {
  const l = tiltCase(), arm = { x: .2, angle: 0 };
  l.r.layer.mode = 'idle'; l.r.layer.items.cup = { held: 'right' };
  l.r.layer.nodes = {
    rightHand: { getWorldPosition: v => Object.assign(v,{x:arm.x,y:1,z:0}),
      getWorldQuaternion: q => Object.assign(q,{x:0,y:Math.sin(arm.angle/2),z:0,w:Math.cos(arm.angle/2)}) },
    rightUpperArm: { getWorldPosition: v => Object.assign(v,{x:0,y:1.3,z:0}) },
  };
  l.world.cups.cup.ml = 100;
  l._sipAngle = ctx.RoomLiquid.prototype._sipAngle.bind(l);
  return { l, arm };
}
test('stable arm may prealign before first contact, limited below critical and at slow angular speed', () => {
  const { l } = alignmentCase();
  for(let i=0;i<100;i++) {const before=l.tiltNow.cup;l._tiltStep(.1);assert.ok(l.tiltNow.cup-before<=.025+1e-10);}
  const target=sipTarget(l.world.cups.cup,capacityMl),limit=Math.min(.35,target.critical*.4);
  assert.ok(Math.abs(l.tiltNow.cup-limit)<1e-10);
  assert.ok(l.tiltNow.cup<target.critical);assert.equal(l.sipping.cup.reached,undefined);
  assert.equal(l.consumption.state.sequence,0); // Alignment is not a liquid-entry event.
});
test('prealignment cannot begin with moving wrist, rotating wrist, missing bones or low pose weight', () => {
  const { l, arm } = alignmentCase();
  for(let i=0;i<30;i++){arm.x+=.01;l._tiltStep(.1);}assert.equal(l.tiltNow.cup,0);
  for(let i=0;i<30;i++){arm.angle+=.1;l._tiltStep(.1);}assert.equal(l.tiltNow.cup,0);
  l.r.poses.W=.8;for(let i=0;i<10;i++)l._tiltStep(.1);assert.equal(l.tiltNow.cup,0);
  l.r.poses.W=1;delete l.r.layer.nodes.rightHand;
  for(let i=0;i<10;i++)l._tiltStep(.1);assert.equal(l.tiltNow.cup,0);
});
test('once lip contact was established, loss returns upright and never resumes blind prealignment', () => {
  const { l } = alignmentCase();
  l.lipGap.cup=.03;for(let i=0;i<10;i++)l._tiltStep(.1);assert.equal(l.sipping.cup.reached,true);assert.ok(l.tiltNow.cup>.35);
  l.lipGap.cup=.07;for(let i=0;i<60;i++)l._tiltStep(.1);
  assert.equal(l.tiltNow.cup,0);assert.equal(l.sipping.cup.phase,'lost-contact');
  l.lipGap.cup=.03;l._tiltStep(.1);assert.ok(l.tiltNow.cup>0);assert.equal(l.sipping.cup.phase,'drinking');
});
test('empty and wrist-budget-unreachable cups cannot enter prealignment', () => {
  for(const ml of [0,1]) {
    const { l } = alignmentCase();l.world.cups.cup.ml=ml;
    for(let i=0;i<40;i++)l._tiltStep(.1);
    assert.equal(l.tiltNow.cup??0,0);assert.equal(l.sipping.cup.target.reachable,false);
    assert.equal(l.tiltGoal.cup,undefined); // Fully upright zero goals are removed.
  }
});
test('prealignment pauses further tipping while the arm moves and resumes only after settling', () => {
  const { l, arm } = alignmentCase();for(let i=0;i<7;i++)l._tiltStep(.1);
  const before=l.tiltNow.cup;assert.ok(before>0);
  for(let i=0;i<10;i++){arm.x+=.02;l._tiltStep(.1);}assert.equal(l.tiltNow.cup,before);
  l._tiltStep(.1);assert.equal(l.tiltNow.cup,before);
  l._tiltStep(.1);assert.ok(l.tiltNow.cup>before);
});

test('exact sip opts are instance-local and limited to actual V8 registered nonphysical cups', () => {
  for(const scenario of ['actual','legacy','old-scene','unknown-item','physical','other-pose']) {
    const {liquid:l}=make();l.r.scene.sceneId=scenario==='old-scene'?'petal-oak-life-v1':'casa-luce-v8';
    l.r.needs.actualConsumption=scenario!=='legacy';
    l.r.layer.items.cup={id:'cup',held:'right',physical:scenario==='physical'};
    if(scenario==='unknown-item')delete l.world.cups.cup;
    l.r.props.stream=()=>{};l.r.props.tilt=()=>{};
    let received;const opts={side:'right'};
    l.r.poses={set:(name,p)=>{received=p;},pose:{}};l._wrap();
    l.r.poses.set(scenario==='other-pose'?'bite':'sip',opts);
    assert.equal(received.sipExact,scenario==='actual'?true:undefined);
    assert.equal(received.sipPalmIn,scenario==='actual'?Math.PI/6:undefined);
    assert.equal(opts.sipExact,undefined);
    if(scenario!=='actual')assert.equal(received,opts);
  }
});
