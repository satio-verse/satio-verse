"""Offline import contract tests; never starts a renderer, model, or service."""
import importlib.util
import json
import math
from pathlib import Path
import struct
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("casa_import", ROOT / "tools/import_casa_luce_v8.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def minimal_glb(doc, binary=b""):
    data=json.dumps(doc).encode();data+=b" "*((-len(data))%4)
    body=struct.pack("<I4s",len(data),b"JSON")+data
    if binary:
        binary+=b"\0"*((-len(binary))%4)
        body+=struct.pack("<I4s",len(binary),b"BIN\0")+binary
    return struct.pack("<4sII",b"glTF",2,12+len(body))+body


class GeometryTests(unittest.TestCase):
    def test_parent_rotation_and_local_bounds_use_all_eight_corners(self):
        d={"nodes":[{"name":"parent","translation":[4,1,-2],"rotation":[0,math.sin(math.pi/4),0,math.cos(math.pi/4)],"children":[1]},
                    {"name":"child","translation":[1,2,0]}]}
        g=M.Glb(minimal_glb(d));b=g.world_bounds("child",{"center":[0,0,0],"size":[2,4,6]})
        for got,want in zip(b["min"],[1,1,-4]):self.assertAlmostEqual(got,want)
        for got,want in zip(b["max"],[7,5,-2]):self.assertAlmostEqual(got,want)

    def test_gltf_matrix_is_column_major(self):
        matrix=list(M.IDENTITY);matrix[12:15]=[2,3,4]
        self.assertEqual(M.transform(M.node_matrix({"matrix":matrix}),[1,2,3]),[3,5,7])

    def test_external_dependencies_and_cycles_fail_closed(self):
        with self.assertRaisesRegex(ValueError,"self-contained"):
            M.Glb(minimal_glb({"nodes":[],"images":[{"uri":"outside.png"}]}))
        with self.assertRaisesRegex(ValueError,"cycle"):
            M.Glb(minimal_glb({"nodes":[{"children":[1]},{"children":[0]}]}))

    def test_invalid_geometry_is_rejected(self):
        for vector in ([1,float("nan"),2],[1,True,2],[1,2]):
            with self.assertRaises(ValueError):M.finite_vector(vector)
        with self.assertRaises(ValueError):M.bounds_from_center([0,0,0],[1,-1,1])

    def test_nav_does_not_cross_inner_wall_or_diagonal_corner(self):
        scene={"floor":[[-2,-2],[2,-2],[2,2],[-2,2]],"agentRadius":.26,"spawn":{"pos":[-1,0]},
               "objects":[],"decor":[{"box":{"center":[0,0],"size":[.1,4],"height":3}}]}
        g=M.Grid(scene)
        self.assertTrue(all(g.point(p)[0]<0 for p in g.connected))
        with self.assertRaises(ValueError):g.approach([1,0],.4)


class ProvenanceTests(unittest.TestCase):
    def test_source_hash_change_is_rejected(self):
        with tempfile.TemporaryDirectory()as tmp:
            root=Path(tmp);(root/"input").write_bytes(b"original")
            (root/"snapshot-manifest.json").write_bytes(M.json_bytes({"files":[{"relative_path":"input","sha256":M.sha(b"original")}]}))
            M.verify_source(root)
            (root/"input").write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError,"mismatch"):M.verify_source(root)

    def test_output_preflight_protects_hand_edits_before_any_write(self):
        with tempfile.TemporaryDirectory()as tmp:
            root=Path(tmp);a=root/"a";b=root/"b";manifest=root/"manifest.json"
            M.write_outputs({a:b"owned",b:b"original"},manifest,{})
            b.write_bytes(b"user edit")
            with self.assertRaises(FileExistsError):M.write_outputs({a:b"new",b:b"new"},manifest,{})
            self.assertEqual(a.read_bytes(),b"owned")
            self.assertEqual(b.read_bytes(),b"user edit")


@unittest.skipUnless(M.DEFAULT_GLB.exists()and(M.DEFAULT_SOURCE/"snapshot-manifest.json").exists(),"Frozen V8 fixture unavailable")
class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source=M.verify_source(M.DEFAULT_SOURCE)
        cls.metadata=M.read_json(M.DEFAULT_SOURCE/"life_sim_v8/assets/room_interactions.json")
        cls.glb=M.Glb(M.DEFAULT_GLB.read_bytes())
        cls.result=M.build(cls.metadata,cls.glb,M.read_json(M.MAPPING),cls.source)
        cls.scene,cls.afford,cls.world,cls.semantics,cls.validation=cls.result

    def test_current_loader_fields_and_no_legacy_bindings(self):
        s=self.scene
        self.assertEqual(s["sceneId"],"casa-luce-v8")
        self.assertEqual(len(s["spawn"]["pos"]),2)
        self.assertIn("yaw",s["spawn"])
        self.assertEqual(len(s["camera"]["room"]),3)
        self.assertEqual(len(s["camera"]["user"]),3)
        self.assertEqual(len(s["drop"]["center"]),2)
        self.assertEqual(len(s["drop"]["spread"]),2)
        self.assertEqual(s["movable"],{})
        self.assertNotIn("petal-oak",json.dumps(s))
        ids={o["id"]for o in s["objects"]}
        self.assertTrue(all(o["target"]in ids for o in s["life"]))
        self.assertIn(s["sleep"]["seat"],ids)
        self.assertTrue(all(node in self.glb.names for node in s["coffee"]["cups"]+s["fridge"]["items"]))

    def test_every_target_and_food_drop_in_spawn_component(self):
        g=M.Grid(self.scene)
        for obj in self.scene["objects"]:
            self.assertLess(math.dist(g.approach(obj["approach"]),obj["approach"]),1e-8,obj["id"])
        p=self.scene["drop"]["center"];spread=self.scene["drop"]["spread"]
        for x in (p[0]-spread[0],p[0]+spread[0]):
            for z in (p[1]-spread[1],p[1]+spread[1]):
                cell=(math.floor((x-g.x0)/g.cell),math.floor((z-g.z0)/g.cell))
                self.assertIn(cell,g.connected)

    def test_high_lintels_keep_body_parts_without_blocking_door_nav(self):
        for d in self.scene["decor"]:
            if "arched lintel"in d["name"]:
                self.assertTrue(d["walkable"])
                self.assertFalse(d.get("wall",False))
                self.assertTrue(any(g["id"]==d["id"]for g in self.scene["collisionParts"]))
            if d["name"]=="Bedroom east partition lower":
                self.assertFalse(d["walkable"])
        parts=[p for g in self.scene["collisionParts"]for p in g["parts"]]
        self.assertEqual(len(parts),len(self.metadata["colliders"])-1)
        self.assertEqual(len({p["source_index"]for p in parts}),len(parts))

    def test_sight_is_world_geometry_and_boxes_are_supported(self):
        s=self.scene["sight"]
        self.assertEqual(s["version"],1)
        self.assertTrue(set(v["zone"]for v in s["regions"].values())<=set(s["zones"]))
        self.assertEqual([k for k,v in s["regions"].items()if v.get("box")],["fridge"])
        self.assertEqual(s["regions"]["fridge"]["at"],self.metadata["utilities_config"]["fridge_interior_center"])
        book=next(v for v in self.semantics["items"]if v["id"]=="Book_01")
        self.assertLess(M.middle(book["boundsWorld"])[0],-3)
        self.assertLess(abs(book["sourceBoundsLocal"]["center"][0]),.01)

    def test_unknown_capabilities_do_not_become_actions_or_rewards(self):
        self.assertEqual([a["id"]for a in self.afford["acts"]],["explore"])
        self.assertEqual(set(self.world["out"]),{"explore"})
        self.assertFalse(self.semantics["completionEventContract"]["emittedByConverter"])
        self.assertTrue(self.semantics["consumables"])
        enabled={c["id"] for c in self.semantics["consumables"] if c["browserConsumptionEnabled"]}
        self.assertEqual(enabled,set(self.scene["coffee"]["cups"]))
        self.assertFalse(self.semantics["browserRuntime"]["foodConsumption"])
        self.assertFalse(self.validation["runtime_verified"])
        self.assertFalse(self.validation["visual_verified"])

    def test_replenishment_targets_are_geometry_bound_with_metered_coffee(self):
        coffee=self.scene["coffee"];bounds=self.glb.bounds(coffee["beansNode"])
        self.assertEqual(coffee["beansNode"],"V4_Coffee_BeanCanister")
        expected=M.middle(bounds);expected[1]=bounds["max"][1]
        self.assertEqual(coffee["beansReach"],expected)
        grid=M.Grid(self.scene)
        self.assertLess(math.dist(grid.approach(coffee["beansStand"]),coffee["beansStand"]),1e-8)
        self.assertLess(abs(coffee["beansStand"][0]-expected[0]),.1)
        self.assertGreater(math.dist(coffee["stand"],coffee["beansStand"]),2)
        self.assertFalse(self.validation["reachTargets"]["beans"]["armPathVerified"])
        acts={a["id"]:a for a in self.scene["life"]}
        self.assertEqual(acts["coffee"]["need"],"beans")
        self.assertEqual(acts["coffee"]["script"],"coffee")
        self.assertNotIn("drink",acts)
        self.assertNotIn("gives",acts["coffee"])
        self.assertEqual(self.scene["extras"]["casa_luce_v8"]["converterVersion"],M.CONVERTER_VERSION)

    def test_non_consumption_living_restores_only_its_own_reviewed_needs(self):
        acts={a["id"]:a for a in self.scene["life"]}
        for ident,expected in {
            "rest":{"gives":{"energy":6},"want":{"energy":1},"stay":[25,50]},
            "read":{"gives":{"energy":2,"fun":4},"want":{"fun":1},"stay":[25,50]},
            "window":{"gives":{"fun":3}},"books":{"gives":{"fun":4}},
            "visit":{"gives":{"social":15,"fun":3},"want":{"social":3},"stay":[8,14]},
            "sleep":{"want":{"energy":5}},
        }.items():
            for key,value in expected.items():self.assertEqual(acts[ident][key],value,(ident,key))
        self.assertEqual(acts["read"]["target"],"reading_chair")
        self.assertEqual(acts["read"]["do"],"sit")
        self.assertIn("休息",acts["read"]["name"])
        self.assertNotIn("gives",acts["computer"])
        self.assertNotIn("gives",acts["sleep"])
        self.assertEqual(self.scene["nutrition"]["capabilities"],{"water":True,"food":False})
        self.assertTrue(self.scene["nutrition"]["actualConsumption"])
        self.assertEqual(self.scene["nutrition"]["feedKinds"],["beans"])
        self.assertTrue(all({"food","water"}.isdisjoint(a.get("gives",{}))for a in acts.values()))
        self.assertTrue(all(not a.get("wave")for a in acts.values()))
        self.assertEqual(set(acts),{"computer","read","rest","window","books","visit","sleep","yoga","coffee","phone","chair_turn","inspect_temperature"})
        self.assertEqual(acts["computer"]["script"],"device_computer")
        self.assertEqual(acts["phone"]["script"],"device_phone")

    def test_fridge_target_has_static_clearance_in_the_upper_compartment(self):
        point=self.scene["fridge"]["reach"];proof=self.validation["reachTargets"]["fridge"]
        radius=proof["radius"]
        interior=M.bounds_from_center(self.metadata["utilities_config"]["fridge_interior_center"],self.metadata["utilities_config"]["fridge_interior_size"])
        self.assertTrue(all(interior["min"][k]+radius-1e-8 <= point[k] <= interior["max"][k]-radius+1e-8 for k in range(3)))
        self.assertGreater(point[1],.7)
        # Independent sphere/AABB distance checks include static shelves, thermometer and every visible bottle.
        boxes=[M.bounds_from_center(c["center"],c["size"])for c in self.metadata["colliders"]]
        boxes.extend(self.glb.bounds(n)for n in self.scene["fridge"]["items"])
        distances=[]
        for box in boxes:
            nearest=[min(box["max"][k],max(box["min"][k],point[k]))for k in range(3)]
            distances.append(math.dist(point,nearest))
        self.assertGreaterEqual(min(distances),radius)
        self.assertAlmostEqual(min(distances),proof["staticClearance"])
        self.assertTrue(proof["doorAssumedOpen"])
        self.assertFalse(proof["armPathVerified"])

    def test_fridge_target_fails_when_geometry_has_no_clear_compartment(self):
        from copy import deepcopy
        metadata=deepcopy(self.metadata)
        metadata["colliders"].append({"name":"test filled interior","center":metadata["utilities_config"]["fridge_interior_center"],"size":metadata["utilities_config"]["fridge_interior_size"]})
        with self.assertRaisesRegex(ValueError,"No clear fridge hand target"):
            M.fridge_reach(metadata,self.glb,self.scene["fridge"]["items"])

    def test_observation_stand_reserves_root_drift_and_head_envelope(self):
        objects={o["id"]:o for o in self.scene["objects"]}
        proof=self.validation["clearancePlanning"]["books"]
        stand=objects["bookcase"]["approach"]
        group=next(g for g in self.scene["collisionParts"]if g["id"]=="bookcase")
        self.assertGreaterEqual(stand[1]-group["max"][2],sum(proof[k]for k in ("arrivalDrift","headAhead","headRadius","margin"))-1e-8)
        self.assertGreaterEqual(proof["estimatedHeadClearance"],proof["margin"])
        self.assertEqual(self.afford["spots"]["bookcase"],stand)
        self.assertEqual(self.scene["sight"]["zones"]["bookcase"],stand)
        self.assertFalse(proof["runtimeVerified"])

    def test_bedside_exit_keeps_real_support_and_lamp_geometry(self):
        objects={o["id"]:o for o in self.scene["objects"]}
        proof=self.validation["clearancePlanning"]["bedsideExit"]
        seat=objects["bed_side"]["seat"];x,z=seat["pos"]
        self.assertAlmostEqual(proof["shift"],.4)
        self.assertAlmostEqual(z,-2.05)
        self.assertEqual(seat,objects["bed"]["seat"])
        self.assertEqual(objects["bed_side"]["approach"],objects["bed"]["approach"])
        self.assertAlmostEqual(objects["bed_side"]["approach"][1],z)
        self.assertGreaterEqual(proof["estimatedHipClearance"],proof["margin"])
        for support in [next(c for c in self.metadata["colliders"]if c["name"]=="Bed mattress support"),next(c for c in self.metadata["placement_surfaces"]if c["name"]=="duvet support")]:
            b=M.bounds_from_center(support["center"],support["size"])
            self.assertTrue(b["min"][0]<=x<=b["max"][0])
            self.assertTrue(b["min"][2]+.15<=z<=b["max"][2]-.15)
        source=next(c for c in self.metadata["colliders"]if c["name"]=="V5 bedside linen shade")
        part=next(p for g in self.scene["collisionParts"]for p in g["parts"]if p["source_name"]==source["name"])
        self.assertEqual({k:part[k]for k in ("min","max")},M.bounds_from_center(source["center"],source["size"]))
        self.assertEqual(self.scene["sleep"]["seat"],"bed_side")
        self.assertFalse(proof["runtimeVerified"])

    def test_build_is_deterministic(self):
        other=M.build(self.metadata,self.glb,M.read_json(M.MAPPING),self.source)
        self.assertEqual(M.json_bytes(self.result),M.json_bytes(other))


if __name__=="__main__":unittest.main()
