"""Deterministic, offline conversion of the frozen Casa Luce V8 integration snapshot.

Never reads private configuration or invokes Blender, a server or model. The input
GLB must be a separately prepared cutaway export from the verified snapshot. Writes
only a new casa-luce-v8 asset directory/profile; existing owned outputs require the
last manifest hash, so hand edits and old rooms cannot be overwritten silently.
"""
from __future__ import annotations

import argparse
from collections import deque
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import re
import struct

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = ROOT / "logs/codex-batch5-20261028/source"
DEFAULT_GLB = ROOT / "logs/codex-batch5-20261028/staging/export/room.glb"
MAPPING = Path(__file__).with_name("data") / "casa-luce-v8-map.json"
CONVERTER_VERSION = 5
IDENTITY = [1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1., 0., 0., 0., 0., 1.]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def finite_vector(value, length=3):
    if not isinstance(value, list) or len(value) != length or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
        raise ValueError("Invalid finite vector")
    return value


def multiply(a, b):
    return [sum(a[k * 4 + row] * b[col * 4 + k] for k in range(4)) for col in range(4) for row in range(4)]


def transform(m, point):
    return [sum(m[k * 4 + row] * point[k] for k in range(3)) + m[12 + row] for row in range(3)]


def node_matrix(node):
    if "matrix" in node:
        return finite_vector(node["matrix"], 16)
    x, y, z, w = finite_vector(node.get("rotation", [0, 0, 0, 1]), 4)
    norm = math.sqrt(x*x + y*y + z*z + w*w)
    if norm < 1e-12:
        raise ValueError("Zero rotation quaternion")
    x, y, z, w = (v/norm for v in (x, y, z, w))
    sx, sy, sz = finite_vector(node.get("scale", [1, 1, 1]))
    t = finite_vector(node.get("translation", [0, 0, 0]))
    return [(1-2*(y*y+z*z))*sx, 2*(x*y+z*w)*sx, 2*(x*z-y*w)*sx, 0,
            2*(x*y-z*w)*sy, (1-2*(x*x+z*z))*sy, 2*(y*z+x*w)*sy, 0,
            2*(x*z+y*w)*sz, 2*(y*z-x*w)*sz, (1-2*(x*x+y*y))*sz, 0, *t, 1]


def aabb(points):
    points = list(points)
    if not points:
        raise ValueError("Empty geometry bounds")
    return {"min": [min(p[k] for p in points) for k in range(3)], "max": [max(p[k] for p in points) for k in range(3)]}


def corners(lo, hi):
    return [[x, y, z] for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]


def bounds_from_center(center, size):
    finite_vector(center); finite_vector(size)
    if any(s <= 0 for s in size):
        raise ValueError("Nonpositive collider size")
    return {"min": [center[k]-size[k]/2 for k in range(3)], "max": [center[k]+size[k]/2 for k in range(3)]}


def middle(b):
    return [(a+c)/2 for a, c in zip(b["min"], b["max"])]


def box2(b):
    c = middle(b)
    return {"center": [c[0], c[2]], "size": [b["max"][0]-b["min"][0], b["max"][2]-b["min"][2]],
            "base": b["min"][1], "height": b["max"][1]-b["min"][1]}


class Glb:
    def __init__(self, data):
        if len(data) < 20 or struct.unpack_from("<4sII", data) != (b"glTF", 2, len(data)):
            raise ValueError("Invalid GLB 2 header")
        count, kind = struct.unpack_from("<I4s", data, 12)
        if kind != b"JSON":
            raise ValueError("Missing JSON chunk")
        self.doc = json.loads(data[20:20+count])
        start = 20+count
        self.binary = b""
        if start < len(data):
            count, kind = struct.unpack_from("<I4s", data, start)
            if kind != b"BIN\x00" or start+8+count != len(data):
                raise ValueError("Unsupported GLB chunks")
            self.binary = data[start+8:]
        if any("uri" in obj for field in ("buffers", "images") for obj in self.doc.get(field, [])):
            raise ValueError("Only self-contained GLBs are accepted")
        self.nodes = self.doc["nodes"]
        self.names = {}
        self.world = {}
        self.parents = {}
        for i, node in enumerate(self.nodes):
            name = node.get("name")
            if name:
                if name in self.names:
                    raise ValueError("Duplicate node name: "+name)
                self.names[name] = i
            for child in node.get("children", []):
                if child in self.parents:
                    raise ValueError("Multiple glTF node parents")
                self.parents[child] = i
        def walk(i, seen):
            if i in seen:
                raise ValueError("Node cycle")
            if i not in self.world:
                p = walk(self.parents[i], seen | {i}) if i in self.parents else IDENTITY
                self.world[i] = multiply(p, node_matrix(self.nodes[i]))
            return self.world[i]
        for i in range(len(self.nodes)):
            walk(i, set())
        self._accessors = {}

    def descendants(self, index):
        yield index
        for child in self.nodes[index].get("children", []):
            yield from self.descendants(child)

    def bounds(self, name):
        points = []
        for i in self.descendants(self.names[name]):
            if "mesh" not in self.nodes[i]:
                continue
            for primitive in self.doc["meshes"][self.nodes[i]["mesh"]]["primitives"]:
                ac = self.doc["accessors"][primitive["attributes"]["POSITION"]]
                if "min" not in ac or "max" not in ac:
                    raise ValueError("POSITION bounds absent")
                points.extend(transform(self.world[i], p) for p in corners(ac["min"], ac["max"]))
        return aabb(points)

    def accessor(self, index):
        if index in self._accessors:
            return self._accessors[index]
        a = self.doc["accessors"][index]
        if "sparse" in a or a.get("normalized"):
            raise ValueError("Sparse/normalized accessor unsupported")
        fmt = {5120:"b",5121:"B",5122:"h",5123:"H",5125:"I",5126:"f"}[a["componentType"]]
        width = {"SCALAR":1,"VEC2":2,"VEC3":3,"VEC4":4}[a["type"]]
        view = self.doc["bufferViews"][a["bufferView"]]
        stride = view.get("byteStride", struct.calcsize(fmt)*width)
        offset = view.get("byteOffset", 0)+a.get("byteOffset", 0)
        result = [struct.unpack_from("<"+fmt*width, self.binary, offset+j*stride) for j in range(a["count"])]
        self._accessors[index] = result
        return result

    def surface_y(self, name, x, z, low, high):
        """Vertical ray against actual triangles, limited to the selected seat band."""
        hits = []
        for i in self.descendants(self.names[name]):
            if "mesh" not in self.nodes[i]:
                continue
            for p in self.doc["meshes"][self.nodes[i]["mesh"]]["primitives"]:
                if p.get("mode", 4) != 4:
                    continue
                positions = [transform(self.world[i], v) for v in self.accessor(p["attributes"]["POSITION"])]
                indices = [v[0] for v in self.accessor(p["indices"])] if "indices" in p else list(range(len(positions)))
                for j in range(0, len(indices), 3):
                    a,b,c = [positions[k] for k in indices[j:j+3]]
                    den = (b[2]-c[2])*(a[0]-c[0])+(c[0]-b[0])*(a[2]-c[2])
                    if abs(den) < 1e-12:
                        continue
                    u = ((b[2]-c[2])*(x-c[0])+(c[0]-b[0])*(z-c[2]))/den
                    v = ((c[2]-a[2])*(x-c[0])+(a[0]-c[0])*(z-c[2]))/den
                    if min(u,v,1-u-v) >= -1e-8:
                        y = u*a[1]+v*b[1]+(1-u-v)*c[1]
                        if low <= y <= high:
                            hits.append(y)
        if not hits:
            raise ValueError("No real support surface for "+name)
        return max(hits)

    def world_bounds(self, node, bounds):
        b = bounds_from_center(bounds["center"], bounds["size"])
        if not node:
            return b
        return aabb(transform(self.world[self.names[node]], p) for p in corners(b["min"], b["max"]))


class Grid:
    """Matches rectangular-profile web/nav.js cell centres, radius, and diagonal rule."""
    def __init__(self, scene):
        self.x0 = min(p[0] for p in scene["floor"]); self.z0 = min(p[1] for p in scene["floor"])
        self.x1 = max(p[0] for p in scene["floor"]); self.z1 = max(p[1] for p in scene["floor"])
        self.cell=.1; self.r=scene["agentRadius"]
        self.w=math.ceil((self.x1-self.x0)/self.cell); self.h=math.ceil((self.z1-self.z0)/self.cell)
        boxes=[o["box"] for o in scene["objects"]+scene["decor"] if "box"in o and not any(o.get(k)for k in ("virtual","wall","walkable"))]
        self.open=set()
        for j in range(self.h):
            for i in range(self.w):
                x,z=self.point((i,j))
                if min(x-self.x0,self.x1-x,z-self.z0,self.z1-z) < self.r:continue
                if any(math.hypot(max(0,abs(x-b["center"][0])-b["size"][0]/2),max(0,abs(z-b["center"][1])-b["size"][1]/2)) < self.r for b in boxes):continue
                self.open.add((i,j))
        start=self.nearest(scene["spawn"]["pos"],self.open)
        self.connected={start};q=deque([start])
        while q:
            i,j=q.popleft()
            for di,dj in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
                p=(i+di,j+dj)
                if p in self.connected or p not in self.open:continue
                if di and dj and ((i+di,j)not in self.open or (i,j+dj)not in self.open):continue
                self.connected.add(p);q.append(p)

    def point(self,p):
        return [self.x0+(p[0]+.5)*self.cell,self.z0+(p[1]+.5)*self.cell]

    def nearest(self,point,cells=None):
        return min(cells if cells is not None else self.connected,key=lambda p:((self.point(p)[0]-point[0])**2+(self.point(p)[1]-point[1])**2,p[1],p[0]))

    def approach(self,point,max_distance=1.0):
        result=self.point(self.nearest(point))
        if math.dist(point,result)>max_distance:raise ValueError(f"No nearby connected approach for {point}: {result}")
        return result


def verify_source(source):
    manifest=read_json(source/"snapshot-manifest.json")
    for entry in manifest["files"]:
        p=source/entry["relative_path"]
        if not p.resolve().is_relative_to(source.resolve()) or sha(p.read_bytes())!=entry["sha256"]:
            raise ValueError("Frozen source mismatch: "+str(p))
    return manifest


def fridge_reach(metadata, glb, drinks, radius=.04):
    """A front-side hand point, preferring the highest clear shelf compartment.

    This is a static sphere/AABB clearance check, not a full arm or carried-item
    path test. The door is intentionally excluded because intoFridge opens it.
    """
    interior=bounds_from_center(metadata["utilities_config"]["fridge_interior_center"],metadata["utilities_config"]["fridge_interior_size"])
    shell=[c for c in metadata["colliders"]if c["name"].startswith("Fridge |")]
    shelves=sorted((bounds_from_center(c["center"],c["size"])for c in shell if "glass shelf"in c["name"]),key=lambda b:b["min"][1])
    roof=next(bounds_from_center(c["center"],c["size"])for c in shell if c["name"]=="Fridge | top exterior")
    bottoms=[interior["min"][1]]+[b["max"][1]for b in shelves]
    tops=[b["min"][1]for b in shelves]+[min(interior["max"][1],roof["min"][1])]
    obstacles=[bounds_from_center(c["center"],c["size"])for c in metadata["colliders"]]+[glb.bounds(node)for node in drinks]
    cx=(interior["min"][0]+interior["max"][0])/2;dx=(interior["max"][0]-interior["min"][0])/4
    for low,high in reversed(list(zip(bottoms,tops))):
        if high-low < 2*radius:continue
        for x in (cx,cx-dx,cx+dx):
            point=[x,(low+high)/2,interior["max"][2]-radius]
            clearance=min(math.sqrt(sum(max(b["min"][k]-point[k],0,point[k]-b["max"][k])**2 for k in range(3)))for b in obstacles)
            if clearance >= radius:
                return point,{"radius":radius,"staticClearance":clearance,"compartment":[low,high],"doorAssumedOpen":True,"armPathVerified":False}
    raise ValueError("No clear fridge hand target in frozen geometry")


def build(metadata, glb, mapping, source_manifest):
    scene_id=mapping["scene_id"]
    if scene_id != "casa-luce-v8":raise ValueError("Output identity must remain casa-luce-v8")
    lo=finite_vector(metadata["room_config"]["bounds_min"]);hi=finite_vector(metadata["room_config"]["bounds_max"])
    spawn=finite_vector(metadata["spawn"])
    colliders=metadata["colliders"]
    if not colliders:raise ValueError("Missing source collision geometry")
    objects=[];decor=[];groups={};assigned=set();by_id={};bounds_by_id={}
    for ident,spec in mapping["objects"].items():
        selected=[(i,c)for i,c in enumerate(colliders)if any(c["name"].startswith(prefix)for prefix in spec["collider_prefixes"])]
        if not selected:raise ValueError("No source colliders for "+ident)
        assigned.update(i for i,c in selected)
        parts=[dict(bounds_from_center(c["center"],c["size"]),part="solid",source_index=i,source_name=c["name"])for i,c in selected]
        bb=aabb(p for part in parts for p in (part["min"],part["max"]))
        groups[ident]={"id":ident,**bb,"parts":parts};bounds_by_id[ident]=bb
        obj={"id":ident,"name":spec["name"],"kind":spec["kind"],"box":box2(bb),"look":middle(bb),"sourceColliders":[i for i,c in selected]}
        objects.append(obj);by_id[ident]=obj
    for i,c in enumerate(colliders):
        if i in assigned or c["name"]=="floor":continue
        bb=bounds_from_center(c["center"],c["size"]);ident=f"geometry_{i:03d}"
        # Inner walls remain physical obstacles; 'wall' is NOT a structural-wall flag.
        walkable=bb["min"][1]>=1.8 or bb["max"][1]<=.08
        obj={"id":ident,"name":c["name"],"kind":"geometry","box":box2(bb),"walkable":walkable,"sourceCollider":i}
        decor.append(obj);groups[ident]={"id":ident,**bb,"parts":[{"part":"solid",**bb,"source_index":i,"source_name":c["name"]}]}
    profile={"sceneId":scene_id,"name":"Casa Luce V8 · 少女的生活小屋","glb":f"/pet/rooms/{scene_id}/room.glb",
             "floor":[[lo[0],lo[2]],[hi[0],lo[2]],[hi[0],hi[2]],[lo[0],hi[2]]],"wallHeight":hi[1],"agentRadius":.26,
             "spawn":{"pos":[spawn[0],spawn[2]],"yaw":0},"camera":{"room":[0,4.2,hi[2]+6],"user":[0,1.45,hi[2]+2]},
             "view":{"pitch":44,"dist":7.2,"lookY":.85,"fov":34,"minDist":1.4,"maxDist":14,"deadzone":.5,
                     "bounds":[[lo[0]-.2,-.2,lo[2]-.2],[hi[0]+.2,hi[1]+.2,hi[2]+.2]],"edge":1.1,"span":2.8},
             "objects":objects,"decor":decor,"collisionParts":list(groups.values()),"movable":{},"carry":{},
             "nutrition":{"enabled":False,"capabilities":{"water":True,"food":False},"actualConsumption":True,"feedKinds":["beans"],"reason":"饮品按实际入口结算；食物动作仍在适配"}}
    grid=Grid(profile);profile["spawn"]["pos"]=grid.approach(profile["spawn"]["pos"],.15)
    for ident,spec in mapping["objects"].items():
        obj=by_id[ident];bb=bounds_by_id[ident];c=middle(bb);fx,fz=spec["front"]
        extent=(bb["max"][0]-bb["min"][0])/2 if fx else (bb["max"][2]-bb["min"][2])/2
        obj["approach"]=grid.approach([c[0]+fx*(extent+.4),c[2]+fz*(extent+.4)])
    # Observation-only stand: reserve the measured arrival/root drift separately
    # from animated head reach and radius. This is not a full-body motion proof.
    watch={"arrivalDrift":.16,"headAhead":.42,"headRadius":.13,"margin":.07}
    book_bounds=bounds_by_id["bookcase"];book_front=book_bounds["max"][2]
    watch_gap=sum(watch.values())
    book_stand=grid.approach([middle(book_bounds)[0],book_front+watch_gap])
    actual_gap=book_stand[1]-book_front
    if actual_gap < watch_gap-1e-8:
        raise ValueError("No bookshelf observation stand with the required head clearance")
    by_id["bookcase"]["approach"]=book_stand
    watch.update(frontZ=book_front,stand=book_stand,actualGap=actual_gap,
                 estimatedHeadClearance=actual_gap-watch["arrivalDrift"]-watch["headAhead"]-watch["headRadius"],
                 runtimeVerified=False)
    def virtual(ident,name,approach,look,place=None):
        o={"id":ident,"name":name,"virtual":True,"approach":grid.approach(approach),"look":look}
        if place is not None:o["place"]=place
        objects.append(o);by_id[ident]=o;return o
    def collider(name):
        return next(c for c in colliders if c["name"]==name)
    seat=collider("Reading linen seat");by_id["reading_chair"]["seat"]={"pos":[seat["center"][0],seat["center"][2]],"yaw":0,"height":seat["center"][1]+seat["size"][1]/2}
    chair_center=transform(glb.world[glb.names["Asset_Chair"]],[0,0,0]);chair_y=glb.surface_y("Asset_Chair",chair_center[0],chair_center[2],.35,.75)
    by_id["desk_chair"]["seat"]={"pos":[chair_center[0],chair_center[2]],"yaw":-math.pi/2,"height":chair_y}
    mattress=collider("Bed mattress support");bedc=mattress["center"]
    mattress_top=glb.surface_y("Asset_Bed",bedc[0],bedc[2],.45,.8)
    # The duvet is static geometry outside Asset_Bed. Its declared support surface
    # must not be confused with the lower mattress-only mesh ray.
    duvet=next(s for s in metadata["placement_surfaces"]if s["name"]=="duvet support")
    bed_top=max(mattress_top,duvet["center"][1]+duvet["size"][1]/2)
    bed_side_x=bedc[0]+mattress["size"][0]/2-.10
    # The recorded stand-up clip moves the root rearward by .4924 m. Keep the
    # original clip/geometry and choose a supported seat farther from the lamp.
    # The .20 m hip envelope is a static planning allowance, not measured bones.
    exit_room={"rearwardRootExcursion":.50,"hipEnvelope":.20,"margin":.20,"step":.10}
    shade=bounds_from_center(collider("V5 bedside linen shade")["center"],collider("V5 bedside linen shade")["size"])
    required_z=shade["max"][2]+exit_room["rearwardRootExcursion"]+exit_room["hipEnvelope"]+exit_room["margin"]
    shift=max(0,math.ceil((required_z-bedc[2]-1e-8)/exit_room["step"]))*exit_room["step"]
    bed_side_z=bedc[2]+shift
    mattress_bounds=bounds_from_center(mattress["center"],mattress["size"])
    duvet_bounds=bounds_from_center(duvet["center"],duvet["size"])
    if not all(b["min"][0] <= bed_side_x <= b["max"][0] and
               b["min"][2]+.15 <= bed_side_z <= b["max"][2]-.15 for b in (mattress_bounds,duvet_bounds)):
        raise ValueError("Bedside clearance candidate has no mattress/duvet support")
    bed_approach=grid.approach([by_id["bed"]["approach"][0],bed_side_z],.15)
    by_id["bed"]["approach"]=bed_approach
    bed_side=virtual("bed_side","床边",bed_approach,[bedc[0],bed_top,bed_side_z])
    bed_side["seat"]={"pos":[bed_side_x,bed_side_z],"yaw":math.pi/2,"height":bed_top}
    exit_room.update(originalZ=bedc[2],seatZ=bed_side_z,shift=shift,shadeFrontZ=shade["max"][2],
                     estimatedHipClearance=bed_side_z-exit_room["rearwardRootExcursion"]-exit_room["hipEnvelope"]-shade["max"][2],
                     footSupportMargin=mattress_bounds["max"][2]-bed_side_z,runtimeVerified=False)
    by_id["bed"]["seat"]=deepcopy(bed_side["seat"])
    virtual("center","房间中间",profile["spawn"]["pos"],[0,1.1,0])
    virtual("user","你",profile["spawn"]["pos"],"camera")
    anchors=metadata["life_config"]["anchors"]
    fridge_pos=middle(glb.bounds("Fridge_Door_Hinge"));fridge_ap=grid.approach([fridge_pos[0],fridge_pos[2]+.65])
    virtual("fridge","冰箱",fridge_ap,fridge_pos)
    yoga=next(s for s in metadata["placement_surfaces"]if s["name"]=="yoga mat");yc=yoga["center"]
    virtual("yoga","瑜伽垫",[yc[0],yc[2]],[yc[0],1.0,yc[2]+1])
    window=middle(glb.bounds("Asset_Window_Study"));virtual("window","窗户",[window[0]+.9,window[2]],window)
    desk_top=collider("Furniture | Desk | 平面.008");dc=desk_top["center"]
    tabletop=collider("Reading round table");tc=tabletop["center"]
    virtual("surf_table","阅读边桌",by_id["reading_table"]["approach"],tc,[tc[0],tc[1]+tabletop["size"][1]/2,tc[2]])
    virtual("surf_desk","电脑桌台面",by_id["desk"]["approach"],dc,[dc[0]+.15,dc[1]+desk_top["size"][1]/2,dc[2]])
    virtual("surf_bed","床面",bed_side["approach"],[bedc[0],bed_top,bedc[2]],[bed_side_x-.15,bed_top,bedc[2]])
    virtual("surf_chair","阅读椅座面",by_id["reading_chair"]["approach"],seat["center"],[seat["center"][0],by_id["reading_chair"]["seat"]["height"],seat["center"][2]])
    virtual("surf_yoga","瑜伽区地面",by_id["yoga"]["approach"],yc,[yc[0],yc[1]+yoga["size"][1]/2,yc[2]])
    profile["sleep"]={"seat":"bed_side","x":[bedc[0]-.8,bedc[0]+.8],"hipsX":bedc[0],"headZ":anchors["sleep_head"][2]+.18,
                      "top":bed_top,"yaw":0,"zFoot":bedc[2]+mattress["size"][2]/2,"note":"V8 geometry-derived candidate; VRM sleep clearance requires runtime qualification."}
    cups=[i["id"]for i in metadata["items"]if i["id"]in ("IR_Item_Espresso_Pink","IR_Item_Espresso_White","IR_Item_Mug_0","IR_Item_Mug_1")]
    profile["coffee"]={"stand":by_id["coffee"]["approach"],"cups":cups[:2],"power":"IR_Coffee_Power","brew":"IR_Coffee_Brew"}
    beans_node="V4_Coffee_BeanCanister";beans_box=glb.bounds(beans_node);beans=middle(beans_box);beans[1]=beans_box["max"][1]
    beans_stand=grid.approach([beans[0],beans_box["max"][2]+.4])
    profile["coffee"].update(beansNode=beans_node,beansReach=beans,beansStand=beans_stand)
    drinks=[i["id"]for i in metadata["items"]if re.fullmatch(r"IR_Item_(Juice|Soda|Bottle)_\d+",i["id"])]
    fridge_point,fridge_proof=fridge_reach(metadata,glb,drinks)
    profile["fridge"]={"items":drinks,"reach":fridge_point};profile["yoga"]={"stand":by_id["yoga"]["approach"],"yaw":0}
    profile["drop"]={"center":profile["spawn"]["pos"],"spread":[.03,.03],"note":"Food drops on the verified open floor cell, not inside a table."}
    def activity(ident,name,target,do="go",script=None):
        o=by_id[target];a={"id":ident,"name":name,"going":"去"+o["name"],"doing":name,"target":target,"do":do,"stay":[12,25],"weight":1}
        if script:a["script"]=script
        if "box"in o:a["pick"]=deepcopy(o["box"])
        return a
    profile["life"]=[activity("computer","坐在电脑前","desk_chair","sit"),activity("read","在阅读椅休息","reading_chair","sit"),
                     activity("rest","坐在床边","bed_side","sit"),activity("window","看看窗外","window"),activity("books","在书架前看看","bookcase"),
                     activity("visit","过来看看你","user"),activity("sleep","上床休息","bed_side",script="sleep"),activity("yoga","做瑜伽","yoga",script="yoga")]
    profile["life"][0]["script"]="device_computer"
    profile["life"] += [activity("coffee","冲咖啡并小口喝","coffee",script="coffee"),
                        activity("inspect_temperature","看看杯子的冷热","coffee",script="inspect_temperature"),
                        activity("phone","拿手机看看","desk",script="device_phone"),
                        activity("chair_turn","给电脑椅调向","desk_chair",script="device_chair")]
    profile["life"][-4].update(need="beans",want={"water":2},weight=.5)
    profile["life"][-3]["weight"]=.15
    profile["life"][-1]["weight"]=.1
    profile["life"][0]["look"]="desk";profile["life"][6]["weight"]=.25;profile["life"][6]["night"]=24
    # Existing non-consumption living semantics. Sitting/resting or companionship
    # can restore their own needs; no water/food is invented for disabled meals.
    living={"rest":{"gives":{"energy":6},"want":{"energy":1},"stay":[25,50]},
            "read":{"gives":{"energy":2,"fun":4},"want":{"fun":1},"stay":[25,50]},
            "window":{"gives":{"fun":3}},"books":{"gives":{"fun":4}},
            "visit":{"gives":{"social":15,"fun":3},"want":{"social":3},"stay":[8,14]},
            "sleep":{"want":{"energy":5}}}
    for a in profile["life"]:a.update(deepcopy(living.get(a["id"],{})))
    zones={"coffee":profile["coffee"]["stand"],"fridge":fridge_ap,"desk":by_id["surf_desk"]["approach"],"bed":bed_side["approach"],
           "reading":by_id["reading_chair"]["approach"],"bookcase":by_id["bookcase"]["approach"],"yoga":by_id["yoga"]["approach"],"window":by_id["window"]["approach"]}
    regions={}
    for ident,zone in [("surf_desk","desk"),("surf_bed","bed"),("surf_table","reading"),("surf_chair","reading"),("surf_yoga","yoga")]:
        regions[ident]={"zone":zone,"at":by_id[ident]["place"],"r":.7 if ident=="surf_bed"else .45}
    cc=[middle(glb.bounds(c))for c in cups[:2]]
    regions["coffee_machine"]={"zone":"coffee","at":[sum(p[k]for p in cc)/len(cc)for k in range(3)],"r":.35}
    regions["fridge"]={"zone":"fridge","at":metadata["utilities_config"]["fridge_interior_center"],"r":.5,"box":True}
    n=by_id["nightstand"]["box"];regions["nightstand"]={"zone":"bed","at":[n["center"][0],n["base"]+n["height"],n["center"][1]],"r":.45}
    names={i["id"]:i.get("label",i["id"])for i in metadata["items"]};names.update({o["id"]:o["name"]for o in objects})
    names.update({"coffee_machine":"咖啡机上","reading":"阅读区","coffee":"咖啡角","bed":"床边","desk":"电脑桌","fridge":"冰箱里"})
    profile["sight"]={"version":1,"zones":zones,"walk":{},"also":{},"regions":regions,"items":cups,"names":names,"kinds":{c:"cup"for c in cups},"kindNames":{"cup":"杯子"},"favorite":"IR_Item_Espresso_Pink"}
    interactions=[]
    for item in metadata["items"]:
        ident=item["id"]
        if ident not in glb.names:raise ValueError("Interaction node missing: "+ident)
        bb=glb.world_bounds(item["node"],item["bounds"])
        interactions.append({"id":ident,"node":item["node"],"label":item.get("label",ident),"type":item["type"],"boundsWorld":bb,
                             "sourceBoundsLocal":item["bounds"],"grip":"book"if ident.startswith("Book_")or ident=="IR_Item_iPhone18ProMax"else None,
                             "browserCapabilities":["render"],"nativeMetadata":deepcopy(item)})
    containers=metadata["life_config"].get("containers",{})
    consumables=[{"id":item["id"],"label":item.get("label",item["id"]),"properties":{**{k:deepcopy(v)for k,v in item.items()if k in ("container","contents","food","liquid","physics","temperature")},
                  **({"nativeContainer":deepcopy(containers[item["id"]])}if item["id"]in containers else{})},
                  "stateAuthority":"browser-liquid ledger; independent of Godot" if item["id"] in cups[:2] else "godot-only; not synchronized", "browserConsumptionEnabled":item["id"] in cups[:2]}for item in metadata["items"]if item["id"]in drinks or item["id"]in containers or any(k in item for k in ("container","food","contents"))]
    semantics={"version":1,"sceneId":scene_id,"coordinateSpace":"world metres +Y up","items":interactions,
               "browserRuntime":{"liquidConsumption":"measured intake; not the imported Godot food model","foodConsumption":False,"deviceControl":"room animation; no operating-system input"},
               "perceptionRegions":[dict(v,boundsWorld=glb.world_bounds(v.get("node"),v),sourceCoordinateSpace="node-local"if v.get("node")else"world")for v in metadata.get("perception_regions",[])],
               "consumables":consumables,"unsupportedFeatures":mapping["unsupported_features"],
               "completionEventContract":{"version":1,"required":["sceneId","actionId","itemId","succeeded","observedBefore","observedAfter","evidence"],
                                          "emittedByConverter":False,"rewardPolicy":"No reward is inferred from metadata, visibility, or attempted action."}}
    profile["extras"]={"casa_luce_v8":{"status":"candidate-not-deployed","converterVersion":CONVERTER_VERSION,"sourceManifestSha256":sha(json_bytes(source_manifest)),
                         "nativeMetadata":f"/pet/rooms/{scene_id}/room_interactions.json","interactions":f"/pet/rooms/{scene_id}/interaction-semantics.json",
                         "lights":metadata["lights"],"consumables":consumables,"unsupportedFeatures":mapping["unsupported_features"],
                         "geometryNotes":["Original sidecar AABBs retained as parts; two convex shapes conservatively bounded, not a transferred Godot solver.",
                                          "Ceiling/high lintels do not block 2D navigation; they remain in body collision geometry.",
                                          "No old room coordinates, rewards, furniture transforms, or saved state were copied."]}}
    books=[i["id"]for i in metadata["items"]if re.fullmatch(r"Book_\d+",i["id"])]
    afford={"sceneId":scene_id,"things":{"books":{"name":"书架上的书","nodes":books,"grip":"book","see":2.4,"seats":["reading_chair","bed_side"]},
           "phone":{"name":"手机","nodes":["IR_Item_iPhone18ProMax"],"grip":"book","see":1.8,"seats":["reading_chair","bed_side"]},
           "bunny":{"name":"小兔子","nodes":["IR_Item_Bunny"],"see":2.2,"seats":["bed_side"]}},
           "spots":{k:v["approach"]for k,v in by_id.items()if k in ("bookcase","window","coffee","desk","bed","center","reading_chair","yoga")},
           "acts":[{"id":"explore","name":"四处看看","going":"在新房间四处看看","doing":"在观察房间","verb":"explore","needs":[],"prior":{"fun":2}}],
           "disabledCapabilities":{"read_book":"V8 pick/read/return path not yet qualified; no real page-reading adapter", "phone":"live device UI remains Godot-only", "hug_bunny":"new geometry reach path pending qualification"}}
    world={"sceneId":scene_id,"note":"Only supported exploration outcomes; no fabricated food/utility task rewards.","out":{"explore":{"fun":1,"per_new":3,"max":10,"energy":-1}}}
    validation={"status":"candidate","source_schema_version":metadata.get("version"),"source_item_count":len(interactions),"source_collider_count":len(colliders),
                "navigation_open_cells":len(grid.open),"navigation_connected_cells":len(grid.connected),"all_approaches_connected":True,
                "supports":{"bed_top":bed_top,"bed_mattress_mesh_top":mattress_top,"desk_chair_top":chair_y,"reading_chair_top":by_id["reading_chair"]["seat"]["height"]},
                "reachTargets":{"fridge":{"point":fridge_point,**fridge_proof},"beans":{"node":beans_node,"point":beans,"stand":beans_stand,"boundsWorld":beans_box,"armPathVerified":False}},
                "clearancePlanning":{"books":watch,"bedsideExit":exit_room},
                "runtime_verified":False,"visual_verified":False,"unsupportedFeatures":mapping["unsupported_features"]}
    return profile,afford,world,semantics,validation


def write_outputs(outputs, manifest_path, manifest):
    previous=read_json(manifest_path).get("outputs",{}) if manifest_path.exists()else{}
    for path,data in outputs.items():
        if path.exists()and path.read_bytes()!=data and previous.get(str(path.resolve()))!=sha(path.read_bytes()):
            raise FileExistsError("Refusing to overwrite an untracked or edited output: "+str(path))
    for path,data in outputs.items():
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    manifest["outputs"]={str(p.resolve()):sha(d)for p,d in outputs.items()}
    manifest_path.parent.mkdir(parents=True,exist_ok=True);manifest_path.write_bytes(json_bytes(manifest))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source",type=Path,default=DEFAULT_SOURCE)
    parser.add_argument("--glb",type=Path,default=DEFAULT_GLB)
    parser.add_argument("--output-root",type=Path,default=ROOT)
    args=parser.parse_args();source=args.source.resolve();root=args.output_root.resolve()
    sm=verify_source(source);mapping=read_json(MAPPING);metadata_bytes=(source/"life_sim_v8/assets/room_interactions.json").read_bytes()
    glb_bytes=args.glb.read_bytes();g=Glb(glb_bytes)
    profile,afford,world,semantics,validation=build(json.loads(metadata_bytes),g,mapping,sm)
    profile["extras"]["casa_luce_v8"]["sourceManifestSha256"]=sha((source/"snapshot-manifest.json").read_bytes())
    asset=root/"desktop/third_party/rooms/casa-luce-v8";scenes=root/"desktop/web/room-scenes"
    outputs={asset/"room.glb":glb_bytes,asset/"room_interactions.json":metadata_bytes,asset/"interaction-semantics.json":json_bytes(semantics),
             asset/"validation.json":json_bytes(validation),scenes/"casa-luce-v8.json":json_bytes(profile),scenes/"casa-luce-v8.afford.json":json_bytes(afford),scenes/"casa-luce-v8.afford-world.json":json_bytes(world)}
    manifest={"version":1,"converterVersion":CONVERTER_VERSION,"sceneId":"casa-luce-v8","status":"candidate-not-deployed","source":str(source),"snapshotManifestSha256":sha((source/"snapshot-manifest.json").read_bytes()),
              "converterSha256":sha(Path(__file__).read_bytes()),"mappingSha256":sha(MAPPING.read_bytes()),"cutawayGlbSha256":sha(glb_bytes),
              "sourceFiles":sm["files"],"runtimeVerified":False,"authorFinalDeliveryClaimed":False}
    write_outputs(outputs,asset/"integration-manifest.json",manifest)
    print(json.dumps({"profile":str(scenes/"casa-luce-v8.json"),"validation":validation},ensure_ascii=False,indent=2))


if __name__=="__main__":
    main()
