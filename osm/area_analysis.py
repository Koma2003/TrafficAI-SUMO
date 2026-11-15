"""Small-area OSM inventory and fact-based reports; no LLM needed."""
import argparse
import hashlib
import html
import json
import math
import os
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

CLASSIFIER_FILE = Path(__file__).with_name('infrastructure_categories.json')
CLASSIFIER = json.loads(CLASSIFIER_FILE.read_text(encoding='utf-8'))
CATEGORIES = {key: value['label'] for key, value in CLASSIFIER.items()}
COLORS = {'residential': '#5776ba', 'social': '#ad4c80', 'commercial': '#d27b25',
          'business': '#7567ad', 'transport': '#21958e', 'leisure': '#4b934f', 'other': '#a6adb5'}
SUBTYPE_LABELS = {
    'amenity=school': 'Школы', 'amenity=kindergarten': 'Детские сады',
    'amenity=university': 'Университеты', 'amenity=college': 'Колледжи',
    'amenity=hospital': 'Больницы', 'amenity=clinic': 'Клиники', 'amenity=doctors': 'Врачебные кабинеты',
    'amenity=pharmacy': 'Аптеки', 'amenity=library': 'Библиотеки', 'amenity=theatre': 'Театры',
    'amenity=restaurant': 'Рестораны', 'amenity=cafe': 'Кафе', 'amenity=bank': 'Банки',
    'amenity=fast_food': 'Быстрое питание', 'amenity=fuel': 'АЗС', 'amenity=marketplace': 'Рынки',
    'amenity=parking': 'Парковки', 'amenity=bus_station': 'Автостанции',
    'shop=supermarket': 'Супермаркеты', 'shop=convenience': 'Продуктовые магазины',
    'shop=mall': 'Торговые центры', 'shop=clothes': 'Магазины одежды',
    'shop=car_parts': 'Автозапчасти', 'shop=electronics': 'Электроника',
    'shop=hardware': 'Хозяйственные магазины', 'shop=doityourself': 'Магазины для ремонта',
    'shop=furniture': 'Мебельные магазины', 'shop=florist': 'Цветочные магазины',
    'building=apartments': 'Многоквартирные здания', 'building=house': 'Жилые дома',
    'building=residential': 'Жилые здания', 'building=detached': 'Отдельные жилые дома',
    'building=school': 'Здания школьного типа', 'building=kindergarten': 'Здания детских садов',
    'building=university': 'Университетские здания', 'building=college': 'Здания колледжей',
    'building=hospital': 'Больничные здания', 'building=clinic': 'Здания клиник',
    'building=library': 'Библиотечные здания', 'building=retail': 'Торговые здания',
    'building=supermarket': 'Здания супермаркетов', 'building=kiosk': 'Киоски как здания',
    'building=train_station': 'Здания вокзалов', 'building=transportation': 'Транспортные здания',
    'building=warehouse': 'Складские здания', 'building=sports_hall': 'Здания спортивных залов',
    'building=stadium': 'Здания стадионов', 'building=grandstand': 'Трибуны',
    'building=yes': 'Здания без указанного назначения', 'landuse=residential': 'Жилые территории',
    'landuse=industrial': 'Промышленные территории', 'landuse=commercial': 'Деловые территории',
    'man_made=works': 'Промышленные предприятия', 'leisure=park': 'Парки',
    'leisure=sports_centre': 'Спортивные центры', 'highway=bus_stop': 'Остановки автобуса',
    'railway=station': 'Железнодорожные станции', 'railway=subway_entrance': 'Входы в метро',
    'public_transport=platform': 'Посадочные платформы', 'public_transport=stop_position': 'Места остановки транспорта',
}


def validate_bbox(bbox):
    """OSM order: south, west, north, east. Limit interactive requests."""
    try:
        south, west, north, east = map(float, bbox)
    except (ValueError, TypeError) as exc:
        raise ValueError('Нужны четыре числовые границы: юг, запад, север, восток') from exc
    if not all(math.isfinite(v) for v in (south, west, north, east)):
        raise ValueError("Координаты должны быть конечными числами")
    if not (-85 <= south < north <= 85 and -180 <= west < east <= 180):
        raise ValueError("Некорректные границы области")
    area = (north - south) * 111.32 * (east - west) * 111.32 * math.cos(math.radians((south + north) / 2))
    if area > 25:
        raise ValueError("Для этого примера выберите область до 25 км²")
    return [south, west, north, east], area


def meaningful(value):
    return value is not None and str(value).strip().lower() not in {'', 'no', 'false', '0'}


def classification_match(tags, classifier=None):
    """Prefer explicit object-use tags; retain building type as a separate basis.

    A school-shaped building containing a shop must not override shop tags.
    The building pass classifies the tagged construction type, not a claim
    that a school/hospital/shop is currently operating there.
    """
    rules = CLASSIFIER if classifier is None else classifier
    for building_pass in (False, True):
        for key, definition in rules.items():
            for tag, accepted in definition['match'].items():
                if (tag == 'building') != building_pass:
                    continue
                value = tags.get(tag)
                if meaningful(value) and (accepted == '*' or value in accepted):
                    return key, 'building_type' if building_pass else 'object_tags', f'{tag}={value}'
    return 'other', 'unclassified', None


def classify(tags, classifier=None):
    return classification_match(tags, classifier)[0]


def known_building_type(tags):
    value = tags.get('building')
    if not meaningful(value) or str(value).strip().lower() in {'yes', 'unknown', 'building', 'generic'}:
        return None
    return str(value)


def subtype(tags):
    matched = classification_match(tags)[2]
    if matched is not None:
        return matched
    for key in ['amenity', 'shop', 'office', 'industrial', 'man_made', 'public_transport',
                'railway', 'highway', 'leisure', 'tourism', 'landuse', 'building']:
        if meaningful(tags.get(key)):
            return f'{key}={tags[key]}'
    return 'unknown'


def object_role(tags):
    poi_keys = ['amenity', 'shop', 'office', 'industrial', 'leisure', 'tourism', 'public_transport']
    if any(meaningful(tags.get(key)) for key in poi_keys) or tags.get('man_made') == 'works' or tags.get('highway') == 'bus_stop' or tags.get('railway') in {'station', 'halt', 'subway_entrance'}:
        return 'poi'
    if meaningful(tags.get('building')):
        return 'building'
    if meaningful(tags.get('landuse')):
        return 'landuse'
    return 'other'


def summarize(data, bbox, source="OpenStreetMap / Overpass API"):
    bbox, area = validate_bbox(bbox)
    if not isinstance(data, dict) or not isinstance(data.get('elements'), list):
        raise ValueError('Ожидается ответ Overpass с массивом elements')
    objects = []
    seen = set()
    for element in data.get("elements", []):
        if not isinstance(element, dict):
            continue
        key = (element.get("type"), element.get("id"))
        if key[0] not in {'node', 'way', 'relation'} or type(key[1]) is not int or key[1] <= 0:
            continue
        if key in seen:
            continue
        seen.add(key)
        tags = element.get("tags", {})
        if not isinstance(tags, dict) or not tags:
            continue
        tags = {str(k): str(v) for k, v in tags.items()}
        position = element.get("center", element)
        lat, lon = position.get('lat'), position.get('lon')
        valid_position = all(isinstance(v, (int, float)) and math.isfinite(v) for v in (lat, lon))
        inside = valid_position and bbox[0] <= lat <= bbox[2] and bbox[1] <= lon <= bbox[3]
        kind = subtype(tags)
        category, basis, _ = classification_match(tags)
        building_type = known_building_type(tags)
        objects.append({"osm_type": key[0], "osm_id": key[1], "category": category,
                        "name": tags.get("name") or "Без названия", "lat": lat if valid_position else None,
                        "lon": lon if valid_position else None, "tags": tags, 'role': object_role(tags),
                        'subtype': kind, 'subtype_label': SUBTYPE_LABELS.get(kind, kind),
                        'category_basis': basis, 'building_type': building_type,
                        'building_category': classify({'building': building_type}) if building_type else None,
                        'representative_point_inside': bool(inside)})
    counts = Counter(o["category"] for o in objects)
    subtypes = Counter(o['subtype'] for o in objects)
    roles = Counter(o['role'] for o in objects)
    bases = Counter(o['category_basis'] for o in objects)
    poi_categories = Counter(o['category'] for o in objects if o['role'] == 'poi' and o['category_basis'] == 'object_tags')
    typed_buildings = Counter(o['building_type'] for o in objects if o['building_type'])
    metadata = data.get('_trafficai', {})
    return {"bbox": bbox, "area_km2": area, "source": metadata.get("provider", source) if source == "OpenStreetMap / Overpass API" else source,
            'retrieved_at': metadata.get('fetched_at'),
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'osm_snapshot_at': data.get('osm3s', {}).get('timestamp_osm_base'),
            'cache_hit': bool(metadata.get('cache_hit', False)),
            'query_sha256': metadata.get('query_sha256'),
            'boundary_semantics': 'OSM objects selected by Overpass bbox; ways and relations may extend outside. Representative centers are not entrances or clipped geometries.',
            'role_counts': {k: roles[k] for k in ['poi', 'building', 'landuse', 'other']},
            'unknown_buildings': sum(o['role'] == 'building' and o['building_type'] is None for o in objects),
            'unclassified_buildings': sum(o['role'] == 'building' and o['building_type'] is not None and o['category'] == 'other' for o in objects),
            'category_basis_counts': dict(bases),
            'poi_category_counts': {key: poi_categories[key] for key in CATEGORIES},
            'building_type_counts': dict(typed_buildings),
            'representative_points_outside': sum(o['lat'] is not None and not o['representative_point_inside'] for o in objects),
            'subtypes': [{'tag': key, 'label': SUBTYPE_LABELS.get(key, key), 'count': count} for key, count in subtypes.most_common()],
            "counts": {k: counts[k] for k in CATEGORIES}, "objects": objects}


def build_query(bbox):
    bbox, _ = validate_bbox(bbox)
    filters = ['building', 'amenity', 'shop', 'office', 'industrial', 'man_made=works',
               'landuse', 'leisure', 'tourism', 'public_transport',
               'railway~"^(station|halt|subway_entrance)$"', 'highway=bus_stop']
    return '[out:json][timeout:45][bbox:' + ','.join(map(str, bbox)) + '];(' + ''.join(f'nwr[{tag}];' for tag in filters) + ');out center tags;'


def fetch_area(bbox, cache_dir):
    query = build_query(bbox)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(query.encode()).hexdigest()
    target = cache_dir / (digest + '.json')
    if target.exists() and datetime.now().timestamp() - target.stat().st_mtime < 86400:
        data = json.loads(target.read_text(encoding='utf-8'))
        metadata = data.setdefault('_trafficai', {})
        # A legacy cache file's mtime is not reliable evidence of its fetch date.
        metadata.setdefault('fetched_at', None)
        metadata.update({'cache_hit': True, 'query_sha256': digest})
        return data
    provider = 'OpenStreetMap / Overpass API'
    try:
        response = requests.post('https://overpass-api.de/api/interpreter', data={"data": query},
                                 headers={"User-Agent": "TrafficAI-SUMO-area-demo/1.0"}, timeout=(5,15))
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or not isinstance(data.get('elements'), list):
            raise ValueError('Overpass вернул некорректный ответ')
        if data.get('remark'):
            raise ValueError('Overpass вернул неполный результат: '+str(data['remark']))
    except (requests.RequestException, ValueError) as primary_error:
        try:
            data = fetch_osm_map(bbox)
            provider = 'OpenStreetMap / OSM Map API'
        except (requests.RequestException, ValueError, ET.ParseError) as fallback_error:
            raise ValueError('Не удалось загрузить область: Overpass и резервный OSM API недоступны. '
                             'Попробуйте меньшую область или повторите позже. '
                             f'Overpass: {primary_error}; OSM: {fallback_error}') from fallback_error
    data['_trafficai'] = {'fetched_at': datetime.now(timezone.utc).isoformat(), 'cache_hit': False,
                         'query_sha256': digest, 'provider': provider}
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return data


def fetch_osm_map(bbox):
    """Fallback inventory from the same public OSM database, not sample data."""
    (south,west,north,east), _ = validate_bbox(bbox)
    response=requests.get('https://api.openstreetmap.org/api/0.6/map',
        params={'bbox':f'{west},{south},{east},{north}'},
        headers={'User-Agent':'TrafficAI-SUMO-area-demo/1.0'},timeout=(5,30))
    response.raise_for_status()
    root=ET.fromstring(response.content)
    positions={n.get('id'):(float(n.get('lat')),float(n.get('lon'))) for n in root.findall('node')}
    ways={w.get('id'):[positions[n.get('ref')] for n in w.findall('nd') if n.get('ref') in positions] for w in root.findall('way')}
    elements=[]
    keys={'building','amenity','shop','office','industrial','landuse','leisure','tourism','public_transport'}
    for element in root:
        if element.tag not in {'node','way','relation'}:continue
        tags={t.get('k'):t.get('v') for t in element.findall('tag')}
        if not (keys.intersection(tags) or tags.get('man_made')=='works' or tags.get('highway')=='bus_stop'
                or tags.get('railway') in {'station','halt','subway_entrance'}):continue
        record={'type':element.tag,'id':int(element.get('id')),'tags':tags}
        if element.tag=='node':
            lat,lon=positions[element.get('id')]
            if not south<=lat<=north or not west<=lon<=east:continue
            record.update(lat=lat,lon=lon)
        else:
            points=ways.get(element.get('id'),[]) if element.tag=='way' else [p for member in element.findall('member')
                for p in (ways.get(member.get('ref'),[]) if member.get('type')=='way' else [positions[member.get('ref')]] if member.get('ref') in positions else [])]
            if points:
                record['center']={'lat':(min(p[0] for p in points)+max(p[0] for p in points))/2,
                                  'lon':(min(p[1] for p in points)+max(p[1] for p in points))/2}
        elements.append(record)
    return {'elements':elements}


def source_link(obj, synthetic=False):
    identifier = f'{obj["osm_type"]}/{obj["osm_id"]}'
    if synthetic:
        return identifier + ' (учебный ID)'
    return f'<a href="https://www.openstreetmap.org/{identifier}">{identifier}</a>'


def overview_svg(summary):
    """Schematic coordinates, not a street map or polygon-footprint rendering."""
    south, west, north, east = summary['bbox']
    points = []
    for obj in summary['objects']:
        lat, lon = obj.get('lat'), obj.get('lon')
        if lat is None or lon is None or not(south <= lat <= north and west <= lon <= east):
            continue
        x = 35+(lon-west)/(east-west)*730
        y = 25+(north-lat)/(north-south)*360
        label = html.escape(obj['name'] + ' — ' + CATEGORIES[obj['category']])
        color = COLORS.get(obj['category'], COLORS['other'])
        points.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="4" fill="{color}"><title>{label}</title></circle>')
    return f'<svg viewBox="0 0 800 430" role="img" aria-label="Схема расположения объектов внутри выбранной области"><rect x="35" y="25" width="730" height="360" fill="#f1f5f9" stroke="#73849b"/><text x="35" y="18" font-size="13">Север ↑ · {north:.5f}</text><text x="35" y="410" font-size="13">{west:.5f}° в.д.</text><text x="650" y="410" font-size="13">{east:.5f}° в.д.</text>{"".join(points)}</svg>'


def category_svg(summary):
    counts = summary['counts']
    largest = max(counts.values(), default=1) or 1
    bars = []
    for index, (key, label) in enumerate(CATEGORIES.items()):
        y = 15+index*35
        count = counts.get(key, 0)
        color = COLORS.get(key, COLORS['other'])
        bars.append(f'<text x="0" y="{y+17}" font-size="13">{html.escape(label)}</text><rect x="265" y="{y}" width="{count/largest*420:.1f}" height="24" rx="3" fill="{color}"/><text x="{275+count/largest*420:.1f}" y="{y+17}" font-size="13">{count}</text>')
    return '<svg viewBox="0 0 760 265" role="img" aria-label="Число объектов OSM по категориям">' + ''.join(bars) + '</svg>'


def report_html(summary):
    # Older saved inventories are upgraded in memory, without inventing provenance.
    objects = [dict(obj) for obj in summary['objects']]
    for obj in objects:
        obj.setdefault('role', object_role(obj.get('tags', {})))
        obj.setdefault('subtype', subtype(obj.get('tags', {})))
        obj.setdefault('category_basis', classification_match(obj.get('tags', {}))[1])
        obj.setdefault('building_type', known_building_type(obj.get('tags', {})))
    counts = summary['counts']
    unknown = summary.get('unknown_buildings', sum(o['role'] == 'building' and o['building_type'] is None for o in objects))
    unclassified = summary.get('unclassified_buildings', sum(o['role'] == 'building' and o['building_type'] is not None and o['category'] == 'other' for o in objects))
    unclassified_note = f' Ещё {unclassified} зданий имеют явно заданный тип, но он не относится к шести основным категориям.' if unclassified else ''
    synthetic = summary['source'].startswith('Учебные')
    known = sum(counts.get(key, 0) for key in CATEGORIES if key != 'other')
    poi = sum(o['role'] == 'poi' for o in objects)
    composition = '; '.join(f'{label.lower()} — {counts[key]}' for key, label in CATEGORIES.items() if key != 'other' and counts.get(key))
    narrative = ('В выгрузке отмечены объекты следующих направлений: ' + composition + '.' if composition else 'Объектов с определённой категорией инфраструктуры в ответе источника не найдено.')
    category_details = []
    for key, label in CATEGORIES.items():
        if key == 'other' or not counts.get(key):
            continue
        selected = [obj for obj in objects if obj['category'] == key]
        kinds = Counter(obj['subtype'] for obj in selected)
        detail = '; '.join(f'{html.escape(SUBTYPE_LABELS.get(tag, tag))} — {count}' for tag, count in kinds.most_common(6))
        building_only = sum(obj['category_basis'] == 'building_type' for obj in selected)
        basis_note = f' Из них {building_only} записей отнесены сюда только по типу здания, без подтверждения действующего учреждения.' if building_only else ''
        named = list(dict.fromkeys(obj['name'] for obj in selected if obj['name'] != 'Без названия'))[:5]
        examples = (' Примеры названий из OSM: ' + ', '.join('«'+html.escape(name)+'»' for name in named) + '.') if named else ' Названия в данных не указаны.'
        category_details.append(f'<article><h3>{html.escape(label)}</h3><p>{detail}.{examples}{basis_note}</p></article>')
    rows = ''.join(f'<tr><td><span class="dot" style="background:{COLORS.get(key, COLORS["other"])}"></span>{html.escape(label)}</td><td>{counts.get(key, 0)}</td></tr>' for key, label in CATEGORIES.items())
    def building_label(obj):
        value = obj.get('building_type')
        if value is None:
            return 'не указан' if meaningful(obj.get('tags', {}).get('building')) else '—'
        return SUBTYPE_LABELS.get('building='+value, 'building='+value)
    basis_labels = {'object_tags': 'теги объекта', 'building_type': 'только тип здания', 'unclassified': 'не классифицировано'}
    object_rows = ''.join(f'<tr><td>{html.escape(obj["name"])}</td><td>{html.escape(CATEGORIES[obj["category"]])}<br><small>{basis_labels[obj["category_basis"]]}</small></td><td>{html.escape(SUBTYPE_LABELS.get(obj["subtype"], obj["subtype"]))}</td><td>{html.escape(building_label(obj))}</td><td>{source_link(obj, synthetic)}</td></tr>' for obj in objects)
    fetched_at = summary.get('retrieved_at') or 'не сохранена в исходном файле'
    generated_at = summary.get('generated_at') or 'не сохранена в старом отчёте'
    snapshot = summary.get('osm_snapshot_at') or 'не указана источником'
    cache_note = 'Использован кэш запроса (до 24 часов).' if summary.get('cache_hit') else ''
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Инфраструктура района — TrafficAI</title>
    <style>body{{font:16px/1.6 system-ui,sans-serif;max-width:1080px;margin:32px auto;padding:24px;color:#172338;background:#fff}}h1{{font-size:34px;line-height:1.2}}h2{{margin-top:36px}}h3{{margin:0 0 8px}}.eyebrow{{color:#52667f;font-size:13px;letter-spacing:.08em}}.stats{{display:flex;gap:12px;flex-wrap:wrap;margin:24px 0}}.stat{{background:#edf3fa;border-radius:8px;padding:14px 20px;flex:1;min-width:150px}}.stat strong{{display:block;font-size:28px}}.note{{background:#fff4de;border-left:4px solid #d69b3d;padding:18px}}.details{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}}article{{border:1px solid #d9e1eb;padding:16px;border-radius:8px}}.meta,footer{{font-size:13px;color:#53667e}}svg{{width:100%;max-width:850px;height:auto}}table{{border-collapse:collapse;width:100%;margin:20px 0;font-size:14px}}td,th{{border-bottom:1px solid #d9e1eb;padding:10px;text-align:left;vertical-align:top}}th{{background:#edf3fa}}.dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px}}a{{color:#245ba0}}@media print{{body{{margin:0;max-width:none;padding:0}}article,.stat{{break-inside:avoid}}h2{{break-after:avoid}}}}</style>
    <p class="eyebrow">TRAFFICAI-SUMO · ИНВЕНТАРИЗАЦИЯ ОТКРЫТЫХ ДАННЫХ</p><h1>Что находится в выбранной области</h1>
    <p>{narrative}</p><div class="stats"><div class="stat"><strong>{summary['area_km2']:.2f} км²</strong>площадь прямоугольника</div><div class="stat"><strong>{len(objects)}</strong>записей OSM всего</div><div class="stat"><strong>{poi}</strong>записей POI с тегами назначения</div><div class="stat"><strong>{known}</strong>записей в шести категориях</div></div>
    <p class="note"><strong>Тип здания и учреждение различаются.</strong> У {unknown} зданий назначение не определено: в данных нет конкретного типа здания, например указан только <code>building=yes</code>.{unclassified_note} Конкретные типы зданий сохранены отдельно. Тег <code>building=school</code> обозначает тип здания и сам по себе не подтверждает действующую школу; так же трактуются больничные и торговые здания. Количество записей OSM не равно количеству физических учреждений: одно место может иметь несколько представлений.</p>
    <h2>Состав и примеры инфраструктуры</h2><div class="details">{''.join(category_details) or '<p>Нет категоризированных объектов.</p>'}</div>
    <h2>Количество записей по категориям</h2>{category_svg(summary)}<table><tr><th>Категория</th><th>Записей OSM</th></tr>{rows}</table>
    <h2>Схема расположения объектов</h2>{overview_svg(summary)}<p class="meta">Это схема координат объектов, без дорожной подложки. Цвета соответствуют категориям таблицы. Наведите на точку, чтобы увидеть название. Для полигонов показан центр ограничивающего прямоугольника OSM, а не вход. Центры за границами выбранного прямоугольника на схеме не показаны.</p>
    <h2>Методика и ограничения</h2><p>Выбрана прямоугольная область: юг {summary['bbox'][0]}, запад {summary['bbox'][1]}, север {summary['bbox'][2]}, восток {summary['bbox'][3]} (WGS84). Выгрузка содержит объекты, выбранные bbox-фильтром Overpass. Здания, территории и отношения OSM могут частично выходить за границы; геометрия не обрезалась. Числа относятся к записям источника, а не к точному числу учреждений внутри административного района.</p>
    <p>Классификация выполняется по тегам; правила находятся в <code>osm/infrastructure_categories.json</code>. Здание и POI внутри него могут учитываться отдельно; остановки также могут иметь несколько записей. Отсутствие объекта в выгрузке не доказывает его отсутствие на местности. Население, посещаемость, обеспеченность жителей и транспортная нагрузка не оценивались. Прогноз пешеходных потоков не выполнялся. Отчёт составлен по данным обычным кодом, без LLM.</p>
    <p class="meta">Источник: {html.escape(summary['source'])}.<br>Данные получены: {html.escape(str(fetched_at))}. {cache_note}<br>Состояние базы OSM по данным Overpass: {html.escape(str(snapshot))}.<br>Отчёт сформирован: {html.escape(str(generated_at))}. Все сохранённые даты — UTC.</p>
    <h2>Все объекты и проверяемые источники</h2><table><tr><th>Название</th><th>Категория и основание</th><th>Тип объекта по тегам</th><th>Тип здания</th><th>Источник</th></tr>{object_rows}</table>
    <footer>© OpenStreetMap contributors — ODbL. <a href="https://www.openstreetmap.org/copyright">Условия использования</a>. Учебные синтетические данные, если они указаны в источнике, не являются выгрузкой реальной местности.</footer></html>'''


def save_report(summary, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / 'infrastructure.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    (output / 'report.html').write_text(report_html(summary), encoding='utf-8')
    return output / 'report.html'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bbox', nargs=4, type=float, required=True, metavar=('S', 'W', 'N', 'E'))
    parser.add_argument('--input', type=Path, help='Offline Overpass JSON fixture')
    parser.add_argument('--output', type=Path, default=Path('runs/area'))
    args = parser.parse_args()
    validate_bbox(args.bbox)
    data = json.loads(args.input.read_text(encoding='utf-8')) if args.input else fetch_area(args.bbox, args.output / 'cache')
    source = 'Учебные синтетические данные, не натурная выгрузка' if args.input else 'OpenStreetMap / Overpass API'
    print(save_report(summarize(data, args.bbox, source), args.output).resolve())


if __name__ == '__main__':
    main()
