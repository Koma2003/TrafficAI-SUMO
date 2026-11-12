#========================================================================
# osm/geometry_builder.py — детерминированный построитель geometry.json.
# shape_latlon — полный polyline каждого подхода (для shape= у <edge>).
# ИСПРАВЛЕНО:
#   - в расчёт идут только дороги (есть highway, не footway/steps/...);
#   - normalize_oneway: oneway=-1 разворачивает way, motorway/кольцо — односторонние;
#   - в geometry.json пишется поле "highway" (для приоритетов в net_builder);
#   - замкнутые way, у которых центр = начало = конец, пропускаются.
#========================================================================

import json, math, os, xml.etree.ElementTree as ET
from collections import defaultdict
import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COORDS_FILE = os.path.join(PROJECT_ROOT, "clicked_coords.json")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "osm", "geometry.json")

USER_AGENT = "GeoTrafficAI/1.0"
BBOX_DEG = 0.005
TRAFFIC_LIGHT_RADIUS = 70.0
ANGLE_CLUSTER_TOLERANCE = 30.0

# Значения highway, которые НЕ являются проезжей частью для машин
NON_ROAD_HIGHWAYS = {
    "footway", "cycleway", "path", "steps", "pedestrian", "bridleway",
    "corridor", "platform", "construction", "proposed", "elevator",
}

DEFAULT_LANES = {
    "motorway": 3, "motorway_link": 1, "trunk": 2, "trunk_link": 1,
    "primary": 2, "primary_link": 1, "secondary": 2, "secondary_link": 1,
    "tertiary": 1, "tertiary_link": 1, "residential": 1, "service": 1,
    "unclassified": 1, "living_street": 1, "pedestrian": 1,
}
DEFAULT_MAXSPEED_KMH = {
    "motorway": 110, "trunk": 90, "primary": 60, "secondary": 60,
    "tertiary": 60, "residential": 40, "service": 20,
    "living_street": 20, "unclassified": 40,
}


def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    dlat = math.radians(lat2 - lat1); dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def normalize_oneway(tags, nodes):
    """
    Возвращает (is_oneway, nodes).
    oneway=-1 / reverse: движение против порядка узлов -> список узлов разворачивается.
    Без тега: односторонние — кольцо и motorway/motorway_link.
    """
    v = str(tags.get("oneway", "")).strip().lower()
    if v in ("-1", "reverse"):
        return True, list(reversed(nodes))
    if v in ("yes", "true", "1"):
        return True, nodes
    if v in ("no", "false", "0", "reversible", "alternating"):
        return False, nodes
    if tags.get("junction") in ("roundabout", "circular"):
        return True, nodes
    if tags.get("highway") in ("motorway", "motorway_link"):
        return True, nodes
    return False, nodes


def parse_lanes(tags):
    raw = tags.get("lanes")
    if raw:
        try:
            n = int(str(raw).split(";")[0])
            if n > 0: return n
        except (ValueError, TypeError): pass
    return DEFAULT_LANES.get(tags.get("highway"), 1)


def parse_maxspeed(tags):
    raw = tags.get("maxspeed")
    if not raw: return DEFAULT_MAXSPEED_KMH.get(tags.get("highway"), 50)
    s = str(raw).strip().lower()
    if s in ("none","signals","walk","variable"): return DEFAULT_MAXSPEED_KMH.get(tags.get("highway"), 50)
    s = s.replace("km/h","").replace("kmh","").strip()
    if s.endswith("mph"):
        try: return round(float(s[:-3].strip()) * 1.60934)
        except ValueError: return DEFAULT_MAXSPEED_KMH.get(tags.get("highway"), 50)
    if ":" in s: return DEFAULT_MAXSPEED_KMH.get(tags.get("highway"), 50)
    try: return int(float(s))
    except ValueError: return DEFAULT_MAXSPEED_KMH.get(tags.get("highway"), 50)


def parse_turn_lanes(tags, lanes):
    raw = tags.get("turn:lanes")
    if not raw: return [["through"] for _ in range(lanes)]
    parts = str(raw).split("|")
    result = []
    for p in parts:
        maneuvers = [m.strip() for m in p.split(";") if m.strip()]
        result.append(maneuvers if maneuvers else ["through"])
    while len(result) < lanes: result.append(["through"])
    return result[:lanes]


def compute_way_angle(nodes_coords, nodes):
    if len(nodes) < 2: return None
    p1 = nodes_coords.get(nodes[0]); p2 = nodes_coords.get(nodes[-1])
    if not p1 or not p2: return None
    return round(math.degrees(math.atan2(p2[1]-p1[1], p2[0]-p1[0])) % 360, 1)


def circular_mean(angles_deg):
    if not angles_deg: return 0.0
    s = sum(math.sin(math.radians(a)) for a in angles_deg)
    c = sum(math.cos(math.radians(a)) for a in angles_deg)
    return round(math.degrees(math.atan2(s, c)) % 360, 1)


def cluster_and_snap(segments):
    by_street = defaultdict(list)
    for seg in segments:
        by_street[seg["street_name"]].append(seg)
    result = []
    for street, segs in by_street.items():
        segs_sorted = sorted(segs, key=lambda s: s["angle_deg"])
        cluster = []
        for seg in segs_sorted:
            if not cluster:
                cluster = [seg]; continue
            diff = abs(((seg["angle_deg"] - cluster[0]["angle_deg"] + 180) % 360) - 180)
            if diff <= ANGLE_CLUSTER_TOLERANCE:
                cluster.append(seg)
            else:
                avg = circular_mean([s["angle_deg"] for s in cluster])
                print(f"  [{street}] кластер {[s['angle_deg'] for s in cluster]} -> {avg}°")
                for s in cluster: s["angle_deg"] = avg; result.append(s)
                cluster = [seg]
        if cluster:
            avg = circular_mean([s["angle_deg"] for s in cluster])
            print(f"  [{street}] кластер {[s['angle_deg'] for s in cluster]} -> {avg}°")
            for s in cluster: s["angle_deg"] = avg; result.append(s)
    return result


def expand_way_to_approaches(way_angle, direction, oneway, base_fields, way_shape):
    """
    way_shape — polyline в естественном порядке way.
    Для основного направления — как есть, для парного — реверс.
    """
    A = way_angle
    A_opp = round((A + 180) % 360, 1)
    result = []
    if direction == "ВЪЕЗД":
        result.append({**base_fields, "angle_deg": A, "direction": "ВЪЕЗД",
                       "shape_latlon": way_shape})
        if not oneway:
            result.append({**base_fields, "angle_deg": A_opp, "direction": "ВЫЕЗД",
                           "shape_latlon": list(reversed(way_shape))})
    elif direction == "ВЫЕЗД":
        result.append({**base_fields, "angle_deg": A, "direction": "ВЫЕЗД",
                       "shape_latlon": way_shape})
        if not oneway:
            result.append({**base_fields, "angle_deg": A_opp, "direction": "ВЪЕЗД",
                           "shape_latlon": list(reversed(way_shape))})
    return result


def dedupe_approaches(approaches):
    merged = {}
    for app in approaches:
        key = (app["street_name"], round(app["angle_deg"]), app["direction"])
        if key not in merged:
            merged[key] = dict(app)
        else:
            m = merged[key]
            m["lanes"] = max(m["lanes"], app["lanes"])
            while len(m["turn_lanes"]) < m["lanes"]:
                m["turn_lanes"].append(["through"])
            while len(m["lane_flow_rates"]) < m["lanes"]:
                m["lane_flow_rates"].append(m["lane_flow_rates"][0] if m["lane_flow_rates"] else 450)
            m["traffic_lights"] = sorted(set(m.get("traffic_lights", [])) | set(app.get("traffic_lights", [])))
            # при слиянии оставляем более длинный shape
            if len(app.get("shape_latlon") or []) > len(m.get("shape_latlon") or []):
                m["shape_latlon"] = app["shape_latlon"]
            if m.get("far_lat") is not None and app.get("far_lat") is not None:
                dm = (m["far_lat"]-m["center_lat"])**2 + (m["far_lon"]-m["center_lon"])**2
                da = (app["far_lat"]-app["center_lat"])**2 + (app["far_lon"]-app["center_lon"])**2
                if da > dm:
                    m["far_lat"] = app["far_lat"]; m["far_lon"] = app["far_lon"]
    return list(merged.values())


def main():
    with open(COORDS_FILE, "r", encoding="utf-8") as f:
        coords = json.load(f)
    my_lat, my_lon = coords["lat"], coords["lon"]
    print(f"Координаты клика: {my_lat}, {my_lon}")

    bbox = f"{my_lon-BBOX_DEG},{my_lat-BBOX_DEG},{my_lon+BBOX_DEG},{my_lat+BBOX_DEG}"
    r = requests.get(f"https://api.openstreetmap.org/api/0.6/map?bbox={bbox}",
                     headers={"User-Agent": USER_AGENT}, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)

    nodes_coords = {n.get("id"): (float(n.get("lat")), float(n.get("lon"))) for n in root.findall("node")}

    # ИСПРАВЛЕНО: только дороги. Здания, заборы, пешеходные дорожки не участвуют
    # ни в поиске перекрёстка, ни в построении подходов.
    all_ways = {}
    skipped = 0
    for w in root.findall("way"):
        wid = w.get("id")
        nds = [nd.get("ref") for nd in w.findall("nd")]
        tags = {t.get("k"): t.get("v") for t in w.findall("tag")}
        hw = tags.get("highway")
        if not hw or hw in NON_ROAD_HIGHWAYS:
            skipped += 1
            continue
        all_ways[wid] = {"nodes": nds, "tags": tags}
    print(f"Дорог для машин: {len(all_ways)} (пропущено не-дорог: {skipped})")

    nodes_to_ways = defaultdict(set)
    for wid, data in all_ways.items():
        for nid in data["nodes"]: nodes_to_ways[nid].add(wid)
    intersection_nodes = {nid for nid, ws in nodes_to_ways.items()
                          if len(ws) >= 2 and nid in nodes_coords}
    if not intersection_nodes:
        raise RuntimeError("Не найдено перекрёстка")

    closest_node = min(intersection_nodes,
                       key=lambda nid: haversine(my_lat, my_lon, nodes_coords[nid][0], nodes_coords[nid][1]))
    center_lat, center_lon = nodes_coords[closest_node]
    print(f"Центр: {closest_node} @ {center_lat:.6f}, {center_lon:.6f}")

    traffic_lights = []
    for n in root.findall("node"):
        tags = {t.get("k"): t.get("v") for t in n.findall("tag")}
        if tags.get("highway") == "traffic_signals":
            tlat, tlon = float(n.get("lat")), float(n.get("lon"))
            if haversine(center_lat, center_lon, tlat, tlon) <= TRAFFIC_LIGHT_RADIUS:
                traffic_lights.append(n.get("id"))
    print(f"Светофоров: {len(traffic_lights)} | "
          f"Тип: {'регулируемый' if traffic_lights else 'нерегулируемый (priority)'}")

    raw_segments = []
    for wid, data in all_ways.items():
        tags = data["tags"]
        # ИСПРАВЛЕНО: oneway=-1 разворачивает список узлов
        oneway, nodes = normalize_oneway(tags, data["nodes"])
        if closest_node not in nodes or len(nodes) < 2: continue

        first, last = nodes[0], nodes[-1]
        if first == last:
            print(f"⚠ way {wid}: замкнутый, начало = конец — пропущен")
            continue

        name = tags.get("name") or tags.get("ref") or "без названия"
        lanes = parse_lanes(tags)
        base = {
            "street_name": name, "lanes": lanes,
            "turn_lanes": parse_turn_lanes(tags, lanes),
            "lane_flow_rates": [450]*lanes,
            "traffic_lights": traffic_lights,
            "oneway": oneway,
            "maxspeed": parse_maxspeed(tags),
            # ИСПРАВЛЕНО: класс дороги нужен net_builder для приоритетов
            "highway": tags.get("highway", ""),
            "center_lat": center_lat, "center_lon": center_lon, "way_id": wid,
        }

        def shape_of(node_list):
            return [list(nodes_coords[nid]) for nid in node_list if nid in nodes_coords]

        if first != closest_node and last != closest_node:
            idx = nodes.index(closest_node)
            left_nodes = nodes[:idx+1]; right_nodes = nodes[idx:]
            al = compute_way_angle(nodes_coords, left_nodes)
            ar = compute_way_angle(nodes_coords, right_nodes)
            if al is not None:
                far = nodes_coords[first]
                raw_segments.append({**base, "angle_deg": al, "direction": "ВЪЕЗД",
                                     "far_lat": far[0], "far_lon": far[1],
                                     "shape_latlon": shape_of(left_nodes)})
            if ar is not None:
                far = nodes_coords[last]
                raw_segments.append({**base, "angle_deg": ar, "direction": "ВЫЕЗД",
                                     "far_lat": far[0], "far_lon": far[1],
                                     "shape_latlon": shape_of(right_nodes)})
        elif last == closest_node:
            a = compute_way_angle(nodes_coords, nodes)
            if a is not None:
                far = nodes_coords[first]
                raw_segments.append({**base, "angle_deg": a, "direction": "ВЪЕЗД",
                                     "far_lat": far[0], "far_lon": far[1],
                                     "shape_latlon": shape_of(nodes)})
        elif first == closest_node:
            a = compute_way_angle(nodes_coords, nodes)
            if a is not None:
                far = nodes_coords[last]
                raw_segments.append({**base, "angle_deg": a, "direction": "ВЫЕЗД",
                                     "far_lat": far[0], "far_lon": far[1],
                                     "shape_latlon": shape_of(nodes)})

    print("\nКластеризация углов:")
    snapped = cluster_and_snap(raw_segments)

    approaches = []
    for seg in snapped:
        approaches.extend(expand_way_to_approaches(
            seg["angle_deg"], seg["direction"], seg["oneway"], seg, seg["shape_latlon"]))
    approaches = dedupe_approaches(approaches)
    approaches.sort(key=lambda a: (a["street_name"], a["angle_deg"], a["direction"]))

    os.makedirs(os.path.dirname(OUTPUT_FILE), exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(approaches, f, ensure_ascii=False, indent=2)

    n_in = sum(1 for a in approaches if a["direction"]=="ВЪЕЗД")
    n_out = sum(1 for a in approaches if a["direction"]=="ВЫЕЗД")
    print(f"\n✅ {OUTPUT_FILE}")
    print(f"   Всего: {len(approaches)} (ВЪЕЗД: {n_in}, ВЫЕЗД: {n_out})")

    by_street = defaultdict(list)
    for a in approaches: by_street[a["street_name"]].append(a)
    print("\nПроверка пар:")
    for street, apps in by_street.items():
        angles = sorted({a["angle_deg"] for a in apps})
        print(f"  {street}: {angles}")
        for angle in angles:
            ins = [a for a in apps if a["angle_deg"]==angle and a["direction"]=="ВЪЕЗД"]
            outs = [a for a in apps if a["angle_deg"]==angle and a["direction"]=="ВЫЕЗД"]
            ok = "✓" if (ins and outs) else "⚠"
            shape_len = (ins[0]["shape_latlon"] if ins else outs[0]["shape_latlon"]) if (ins or outs) else []
            hw = (ins[0] if ins else outs[0]).get("highway", "") if (ins or outs) else ""
            print(f"    {angle:>6.1f}°: ВЪЕЗД={len(ins)}, ВЫЕЗД={len(outs)}, "
                  f"точек в shape={len(shape_len)}, highway={hw}  {ok}")


if __name__ == "__main__":
    main()