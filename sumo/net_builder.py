#========================================================================
# Строит сеть SUMO из geometry.json.
# ИСПРАВЛЕНО:
#   - пешеходные связи определяются по function="crossing|walkingarea",
#     а не по подстроке "_c"/"_w" в id (улица «Чехова» больше не ломается);
#   - тротуары/переходы строятся и на быстрых дорогах (после внедрения maxspeed);
#   - netconvert: при неизвестной опции повтор без неё.
#========================================================================

import os, subprocess, math, tempfile, sys, io, re, json, xml.etree.ElementTree as ET

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

APPROACH_LENGTH = 150.0
LANE_OFFSET = 3.0
DEFAULT_MAXSPEED_KMH = 50

# Тротуары/переходы строим на дорогах со скоростью до 80 км/ч
SIDEWALK_MAX_SPEED_MS = 80 / 3.6
# Тротуары и на медленных дорогах (service 20 км/ч = 5.56 м/с)
SIDEWALK_MIN_SPEED_MS = 2.0

# Приоритеты дорог (шкала SUMO). Используются только на НЕрегулируемом перекрёстке.
HIGHWAY_PRIORITY = {
    "motorway": 14, "motorway_link": 13,
    "trunk": 12, "trunk_link": 11,
    "primary": 10, "primary_link": 9,
    "secondary": 8, "secondary_link": 7,
    "tertiary": 6, "tertiary_link": 5,
    "unclassified": 4,
    "residential": 4,
    "service": 2, "living_street": 2,
}


def transliterate(text):
    mapping = {'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z',
               'и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
               'с':'s','т':'t','у':'u','ф':'f','х':'h','ц':'ts','ч':'ch','ш':'sh','щ':'sch',
               'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',' ':'_'}
    result = text.lower()
    for rus, lat in mapping.items(): result = result.replace(rus, lat)
    return re.sub(r'[^a-z0-9_]', '', result)


def project_to_meters(lat, lon, center_lat, center_lon):
    m_lat = 110540.0
    m_lon = 111320.0 * math.cos(math.radians(center_lat))
    return (lon - center_lon) * m_lon, (lat - center_lat) * m_lat


def offset_polyline(shape_xy, offset_mag, ramp_from_far):
    n = len(shape_xy)
    if n < 2: return shape_xy
    result = []
    for i, (x, y) in enumerate(shape_xy):
        if i == 0:
            dx = shape_xy[1][0]-x; dy = shape_xy[1][1]-y
        elif i == n-1:
            dx = x-shape_xy[i-1][0]; dy = y-shape_xy[i-1][1]
        else:
            dx = shape_xy[i+1][0]-shape_xy[i-1][0]
            dy = shape_xy[i+1][1]-shape_xy[i-1][1]
        length = math.hypot(dx, dy)
        if length == 0:
            result.append((x, y)); continue
        px = dy / length; py = -dx / length
        t = (1 - i/(n-1)) if ramp_from_far else (i/(n-1))
        result.append((x + px*offset_mag*t, y + py*offset_mag*t))
    return result


def compute_priority(group):
    """Приоритет группы (street, angle): по highway, иначе по числу полос."""
    hw = (group.get("highway") or "").lower()
    if hw in HIGHWAY_PRIORITY:
        return HIGHWAY_PRIORITY[hw]
    lanes = max(group.get("lanes_in", 0), group.get("lanes_out", 0))
    if lanes >= 3: return 8
    if lanes >= 2: return 6
    return 4


def pedestrian_link_indices(root):
    """
    Индексы пешеходных связей (переходы и walking areas) в tlLogic.
    ИСПРАВЛЕНО: определяем по атрибуту function у рёбер, а не по подстроке в id.
    """
    ped_edges = {e.get("id") for e in root.findall("edge")
                 if e.get("function") in ("crossing", "walkingarea")}
    idx = set()
    for conn in root.findall("connection"):
        if conn.get("from") in ped_edges or conn.get("to") in ped_edges:
            li = conn.get("linkIndex")
            if li is not None and li != "-1":
                try: idx.add(int(li))
                except ValueError: pass
    return idx


def add_pedestrian_phases_to_net(net_file):
    """
    На светофоре: во всех автомобильных фазах пешеходные связи красные,
    плюс отдельная пешеходная фаза (15 c) и общий красный (1 c).
    """
    try:
        tree = ET.parse(net_file); root = tree.getroot()
        ped_idx = pedestrian_link_indices(root)
        tls = root.findall("tlLogic")
        if not tls:
            print("ℹ tlLogic нет (нерегулируемый перекрёсток) — пешеходные фазы не нужны")
            return
        if not ped_idx:
            print("⚠ Пешеходные связи не найдены — пешеходной фазы не будет")
            return
        for tl in tls:
            phases = tl.findall("phase")
            if not phases: continue
            n = len(phases[0].get("state"))
            idx = {i for i in ped_idx if i < n}
            for ph in phases:
                s = list(ph.get("state"))
                for i in idx: s[i] = 'r'
                ph.set("state", "".join(s))
            ped = list("r"*n)
            for i in idx: ped[i] = "G"
            tl.append(ET.Element("phase", duration="15.0", state="".join(ped)))
            tl.append(ET.Element("phase", duration="1.0", state="r"*n))
        tree.write(net_file, encoding="utf-8", xml_declaration=True)
        print(f" Пешеходные фазы добавлены (пешеходных связей: {len(ped_idx)})")
    except Exception as e:
        print(f" Ошибка пешеходных фаз: {e}")


def run_netconvert(base_cmd, optional_opts, tls_opts):
    """
    Запускает netconvert. Если какая-то из необязательных опций (optional_opts —
    список [имя, значение]) не поддерживается, убирает её и повторяет.
    """
    opts = [list(o) for o in optional_opts]
    for attempt in range(len(optional_opts) + 1):
        cmd = list(base_cmd)
        for name, val in opts:
            cmd += [name, val]
        cmd += tls_opts
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result
        err = (result.stderr or "").lower()
        if not opts or "option" not in err:
            return result
        bad = [o for o in opts if o[0].lstrip("-").lower() in err]
        if not bad:
            bad = list(opts)
        for o in bad:
            print(f"⚠ netconvert не принял опцию {o[0]} — убираю и повторяю")
            opts.remove(o)
    return result


def build_network(approaches, output_file="intersection.net.xml", debug=True):
    has_lights = any(a.get("traffic_lights") for a in approaches)
    center_type = "traffic_light" if has_lights else "priority"
    print(f"Тип перекрёстка: {center_type}")

    center_lat, center_lon = None, None
    for a in approaches:
        if a.get("center_lat") is not None:
            center_lat, center_lon = a["center_lat"], a["center_lon"]; break
    use_real = center_lat is not None
    if use_real:
        print(f"Центр: {center_lat:.6f}, {center_lon:.6f}")
    else:
        print(f"Fallback: прямые по {APPROACH_LENGTH:.0f} м")

    grouped = {}
    for app in approaches:
        name = app["street_name"]; angle = app["angle_deg"]
        key = (name, round(angle))
        if key not in grouped:
            grouped[key] = {
                "street_name": name, "angle_deg": angle,
                "lanes_in": 0, "lanes_out": 0, "turn_lanes_in": [],
                "traffic_lights": [], "has_in": False, "has_out": False,
                "oneway": bool(app.get("oneway", False)),
                "maxspeed_in_kmh": DEFAULT_MAXSPEED_KMH,
                "maxspeed_out_kmh": DEFAULT_MAXSPEED_KMH,
                "highway": app.get("highway", ""),
                "in_shape": None, "out_shape": None,
                "in_far": None, "out_far": None,
            }
        g = grouped[key]
        if app["direction"] == "ВЪЕЗД":
            g["lanes_in"] = app["lanes"]
            g["turn_lanes_in"] = app.get("turn_lanes", [])
            g["has_in"] = True
            g["maxspeed_in_kmh"] = app.get("maxspeed") or DEFAULT_MAXSPEED_KMH
            if app.get("shape_latlon"): g["in_shape"] = app["shape_latlon"]
            if app.get("far_lat") is not None: g["in_far"] = (app["far_lat"], app["far_lon"])
        elif app["direction"] == "ВЫЕЗД":
            g["lanes_out"] = app["lanes"]
            g["has_out"] = True
            g["maxspeed_out_kmh"] = app.get("maxspeed") or DEFAULT_MAXSPEED_KMH
            if app.get("shape_latlon"): g["out_shape"] = app["shape_latlon"]
            if app.get("far_lat") is not None: g["out_far"] = (app["far_lat"], app["far_lon"])
        if app.get("traffic_lights"):
            g["traffic_lights"].extend(app["traffic_lights"])
        # highway может прийти не у всех подходов; берём самый старший
        if app.get("highway"):
            cur = (g.get("highway") or "").lower()
            new = app["highway"].lower()
            if HIGHWAY_PRIORITY.get(new, 0) > HIGHWAY_PRIORITY.get(cur, 0):
                g["highway"] = new

    for (name, ang), g in grouped.items():
        if g["oneway"]: continue
        mirror = round((g["angle_deg"]+180) % 360)
        if (name, mirror) not in grouped:
            print(f"⚠ '{name}' ({g['angle_deg']}°): нет пары {mirror}°")
            continue
        if not (g["has_in"] and g["has_out"]):
            print(f"⚠ '{name}' ({g['angle_deg']}°): отсутствует направление")

    for g in grouped.values():
        g["priority"] = compute_priority(g)

    sorted_apps = sorted(grouped.values(), key=lambda x: x["angle_deg"])

    if not has_lights:
        print("\nПриоритеты дорог (главная = больше число):")
        for g in sorted_apps:
            print(f"  {g['street_name']:<30} {g['angle_deg']:>6.1f}°  "
                  f"highway={g['highway'] or '—':<12} priority={g['priority']}")

    # Подготовка shape
    for app in sorted_apps:
        for direction, shape_key, far_key, ramp in [
            ("in", "in_shape", "in_far", True),
            ("out", "out_shape", "out_far", False),
        ]:
            shape_ll = app.get(shape_key)
            if shape_ll and use_real:
                raw = [project_to_meters(lat, lon, center_lat, center_lon) for lat, lon in shape_ll]
                app[f"{direction}_shape_xy"] = offset_polyline(raw, LANE_OFFSET, ramp)
            elif use_real and app.get(far_key):
                fx, fy = project_to_meters(app[far_key][0], app[far_key][1], center_lat, center_lon)
                rad = math.radians(app["angle_deg"])
                ox = LANE_OFFSET*math.cos(rad); oy = -LANE_OFFSET*math.sin(rad)
                if direction == "in":
                    app[f"{direction}_shape_xy"] = [(fx+ox, fy+oy), (0.0, 0.0)]
                else:
                    app[f"{direction}_shape_xy"] = [(0.0, 0.0), (fx+ox, fy+oy)]
            else:
                rad = math.radians(app["angle_deg"])
                ox = LANE_OFFSET*math.cos(rad); oy = -LANE_OFFSET*math.sin(rad)
                if direction == "in":
                    fx = -APPROACH_LENGTH*math.sin(rad)
                    fy = -APPROACH_LENGTH*math.cos(rad)
                    app[f"{direction}_shape_xy"] = [(fx+ox, fy+oy), (0.0, 0.0)]
                else:
                    rad_out = math.radians(app["angle_deg"]+180)
                    fx = -APPROACH_LENGTH*math.sin(rad_out)
                    fy = -APPROACH_LENGTH*math.cos(rad_out)
                    app[f"{direction}_shape_xy"] = [(0.0, 0.0), (fx+ox, fy+oy)]

    nod_xml = '<?xml version="1.0" encoding="UTF-8"?>\n<nodes>\n'
    nod_xml += f'    <node id="center" x="0.0" y="0.0" type="{center_type}"/>\n'
    for app in sorted_apps:
        name = app["street_name"]; angle = app["angle_deg"]
        bid = f"{transliterate(name)}_{angle:.0f}"
        if app["has_in"]:
            x, y = app["in_shape_xy"][0]
            nod_xml += f'    <node id="in_{bid}" x="{x:.2f}" y="{y:.2f}"/>\n'
        if app["has_out"]:
            x, y = app["out_shape_xy"][-1]
            nod_xml += f'    <node id="out_{bid}" x="{x:.2f}" y="{y:.2f}"/>\n'
    nod_xml += '</nodes>\n'

    def shape_attr(xy_list):
        return " ".join(f"{x:.2f},{y:.2f}" for x, y in xy_list)

    edg_xml = '<?xml version="1.0" encoding="UTF-8"?>\n<edges>\n'
    for app in sorted_apps:
        name = app["street_name"]; angle = app["angle_deg"]
        bid = f"{transliterate(name)}_{angle:.0f}"
        prio_attr = f' priority="{app["priority"]}"' if not has_lights else ""
        if app["has_in"]:
            speed_kmh = app["maxspeed_in_kmh"]
            speed = speed_kmh / 3.6
            shp = shape_attr(app["in_shape_xy"])
            edg_xml += (f'    <edge from="in_{bid}" to="center" id="in_{bid}" '
                        f'numLanes="{app["lanes_in"]}" speed="{speed:.2f}"'
                        f'{prio_attr} shape="{shp}"/>\n')
            print(f"  [edge] in_{bid}: {app['lanes_in']} полос(ы), {speed_kmh} км/ч, "
                  f"shape={len(app['in_shape_xy'])} точек"
                  f"{' prio='+str(app['priority']) if not has_lights else ''}")
        if app["has_out"]:
            speed_kmh = app["maxspeed_out_kmh"]
            speed = speed_kmh / 3.6
            shp = shape_attr(app["out_shape_xy"])
            edg_xml += (f'    <edge from="center" to="out_{bid}" id="out_{bid}" '
                        f'numLanes="{app["lanes_out"]}" speed="{speed:.2f}"'
                        f'{prio_attr} shape="{shp}"/>\n')
            print(f"  [edge] out_{bid}: {app['lanes_out']} полос(ы), {speed_kmh} км/ч, "
                  f"shape={len(app['out_shape_xy'])} точек"
                  f"{' prio='+str(app['priority']) if not has_lights else ''}")
    edg_xml += '</edges>\n'

    con_xml = '<?xml version="1.0" encoding="UTF-8"?>\n<connections>\n'
    for app in sorted_apps:
        if not app["has_in"]: continue
        name = app["street_name"]; angle = app["angle_deg"]
        bid = f"{transliterate(name)}_{angle:.0f}"
        in_edge = f"in_{bid}"
        lanes_in = app["lanes_in"]
        turn_lanes = app["turn_lanes_in"] if app["turn_lanes_in"] else [["UNK"]]*lanes_in
        turn_lanes = turn_lanes[:lanes_in]

        for lane_idx, maneuvers in enumerate(turn_lanes):
            sumo_lane = lanes_in - 1 - lane_idx
            for m in maneuvers:
                if m in ("through","UNK"):
                    for other in sorted_apps:
                        if not other["has_out"] or other["street_name"] != name: continue
                        oa = (other["angle_deg"]+180) % 360
                        ta = (angle+180) % 360
                        if abs(oa-ta) < 30:
                            obid = f"{transliterate(other['street_name'])}_{other['angle_deg']:.0f}"
                            tl = min(sumo_lane, other["lanes_out"]-1)
                            con_xml += f'    <connection from="{in_edge}" to="out_{obid}" fromLane="{sumo_lane}" toLane="{tl}" dir="s"/>\n'
                            break
                elif m == "left":
                    ta = (angle+90) % 360; best, bd = None, 50
                    for other in sorted_apps:
                        if not other["has_out"] or other["street_name"]==name: continue
                        oa = (other["angle_deg"]+180) % 360
                        diff = abs((oa-ta+180) % 360 - 180)
                        if diff < bd:
                            bd = diff
                            obid = f"{transliterate(other['street_name'])}_{other['angle_deg']:.0f}"
                            best = (f"out_{obid}", other["lanes_out"])
                    if best:
                        con_xml += f'    <connection from="{in_edge}" to="{best[0]}" fromLane="{sumo_lane}" toLane="{best[1]-1}" dir="l"/>\n'
                elif m in ("right","slight_right"):
                    ta = (angle-90) % 360; best, bd = None, 50
                    for other in sorted_apps:
                        if not other["has_out"] or other["street_name"]==name: continue
                        oa = (other["angle_deg"]+180) % 360
                        diff = abs((oa-ta+180) % 360 - 180)
                        if diff < bd:
                            bd = diff
                            obid = f"{transliterate(other['street_name'])}_{other['angle_deg']:.0f}"
                            best = (f"out_{obid}", other["lanes_out"])
                    if best:
                        con_xml += f'    <connection from="{in_edge}" to="{best[0]}" fromLane="{sumo_lane}" toLane="0" dir="r"/>\n'
    con_xml += '</connections>\n'

    with tempfile.TemporaryDirectory() as tmpdir:
        for name, content in [("nod.xml", nod_xml), ("edg.xml", edg_xml), ("con.xml", con_xml)]:
            with open(os.path.join(tmpdir, f"intersection.{name}"), "w", encoding="utf-8") as f:
                f.write(content)
        base_cmd = [
            "netconvert",
            "--node-files", os.path.join(tmpdir, "intersection.nod.xml"),
            "--edge-files", os.path.join(tmpdir, "intersection.edg.xml"),
            "--connection-files", os.path.join(tmpdir, "intersection.con.xml"),
            "--output-file", output_file,
            "--no-turnarounds",
            "--sidewalks.guess",
            "--crossings.guess",
            "--default.sidewalk-width", "2.0",
        ]
        # Необязательные опции: расширяют тротуары/переходы на быстрые и медленные дороги.
        optional_opts = [
            ["--sidewalks.guess.max-speed", f"{SIDEWALK_MAX_SPEED_MS:.2f}"],
            ["--sidewalks.guess.min-speed", f"{SIDEWALK_MIN_SPEED_MS:.2f}"],
            ["--crossings.guess.speed-threshold", f"{SIDEWALK_MAX_SPEED_MS:.2f}"],
        ]
        tls_opts = []
        if has_lights:
            tls_opts = ["--tls.guess", "--tls.green.time=30", "--tls.yellow.time=3"]
        print(f"Запуск netconvert (lights={has_lights})...")
        result = run_netconvert(base_cmd, optional_opts, tls_opts)
        if result.returncode != 0:
            print("STDERR:", result.stderr)
            raise RuntimeError(f"netconvert error: {result.stderr}")

    add_pedestrian_phases_to_net(output_file)
    return output_file


# ---------------------------------------------------------------------------
# УСТАРЕЛО: эти две функции больше не используются в пайплайне
# (переходы создаёт netconvert --crossings.guess, пешеходы — в route_builder).
# Оставлены, чтобы не ломать возможные импорты.
# ---------------------------------------------------------------------------
def generate_pedestrians_add(approaches, output_file="pedestrians.add.xml"):
    has_lights = any(a.get("traffic_lights") for a in approaches)
    tl_attr = ' trafficLight="true"' if has_lights else ''
    add_xml = '<?xml version="1.0" encoding="UTF-8"?>\n<additional>\n'
    add_xml += '    <pedestrianCrossing id="pc_center" edges=":center_0_0 :center_0_1" />\n'
    for app in approaches:
        if app["direction"] != "ВЪЕЗД": continue
        bid = f"{transliterate(app['street_name'])}_{app['angle_deg']:.0f}"
        edge_id = f"in_{bid}"
        lanes = app["lanes"]
        for lane in range(lanes):
            to_lane = lanes - 1 - lane
            add_xml += (f'    <crossing id="cross_{edge_id}_{lane}" edge="{edge_id}" '
                        f'fromLane="{lane}" toLane="{to_lane}" priority="true" '
                        f'width="3.0"{tl_attr}/>\n')
    add_xml += '</additional>\n'
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(add_xml)
    return output_file


def generate_tls_with_pedestrian_phases(approaches, net_file, output_file="tls_pedestrians.add.xml"):
    try:
        tree = ET.parse(net_file); root = tree.getroot()
        tl = root.find("tlLogic")
        if tl is None:
            return output_file
        ped_idx = pedestrian_link_indices(root)
        if not ped_idx: return output_file
        phases = [{"duration": float(p.get("duration")), "state": p.get("state")} for p in tl.findall("phase")]
        if not phases: return output_file
        n = len(phases[0]["state"])
        ped = list("r"*n)
        for i in ped_idx:
            if i < n: ped[i] = "G"
        phases.append({"duration": 15.0, "state": "".join(ped)})
        phases.append({"duration": 1.0, "state": "r"*n})
        tl_id = tl.get("id")
        xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<additional>\n    <tlLogic id="{tl_id}" type="static" programID="1" offset="0">\n'
        for ph in phases:
            xml += f'        <phase duration="{ph["duration"]}" state="{ph["state"]}"/>\n'
        xml += '    </tlLogic>\n</additional>'
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(xml)
        return output_file
    except Exception:
        return output_file


if __name__ == "__main__":
    from pathlib import Path
    script_dir = Path(__file__).parent.parent
    json_path = script_dir / "osm" / "geometry.json"
    if not json_path.exists():
        print(f" Файл {json_path} не найден!"); exit(1)
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    test_approaches = data if isinstance(data, list) else data.get("approaches", [])
    if not test_approaches:
        print(" Неподдерживаемый формат"); exit(1)
    print(f"Загружено {len(test_approaches)} подходов")
    output_file = script_dir / "sumo" / "test_intersection.net.xml"
    build_network(test_approaches, str(output_file))
    print(f"✅ Сеть: {output_file}")
