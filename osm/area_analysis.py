"""Small-area OSM inventory and fact-based reports; no LLM needed."""
import argparse
import hashlib
import html
import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

CATEGORIES = {
    "residential": "Жилая застройка", "social": "Социальная инфраструктура",
    "commercial": "Магазины и услуги", "business": "Промышленность и офисы",
    "transport": "Транспорт", "leisure": "Досуг и рекреация", "other": "Прочие здания и объекты",
}


def validate_bbox(bbox):
    """OSM order: south, west, north, east. Limit interactive requests."""
    south, west, north, east = map(float, bbox)
    if not all(math.isfinite(v) for v in (south, west, north, east)):
        raise ValueError("Координаты должны быть конечными числами")
    if not (-85 <= south < north <= 85 and -180 <= west < east <= 180):
        raise ValueError("Некорректные границы области")
    area = (north - south) * 111.32 * (east - west) * 111.32 * math.cos(math.radians((south + north) / 2))
    if area > 25:
        raise ValueError("Для этого примера выберите область до 25 км²")
    return [south, west, north, east], area


def classify(tags):
    amenity = tags.get("amenity", "")
    if amenity in {"school", "kindergarten", "university", "college", "hospital", "clinic", "doctors", "pharmacy", "library", "theatre", "community_centre"}:
        return "social"
    if tags.get("shop") or amenity in {"marketplace", "restaurant", "cafe", "bank", "fast_food", "fuel"}:
        return "commercial"
    if tags.get("railway") in {"station", "halt", "subway_entrance"} or tags.get("public_transport") or amenity == "bus_station" or tags.get("highway") == "bus_stop":
        return "transport"
    if tags.get("office") or tags.get("industrial") or tags.get("landuse") in {"industrial", "commercial"} or tags.get("building") in {"industrial", "commercial", "office"}:
        return "business"
    if tags.get("leisure") or tags.get("tourism"):
        return "leisure"
    if tags.get("building") in {"apartments", "house", "residential", "detached", "dormitory", "terrace"} or tags.get("landuse") == "residential":
        return "residential"
    return "other"


def summarize(data, bbox, source="OpenStreetMap / Overpass API"):
    bbox, area = validate_bbox(bbox)
    objects = []
    seen = set()
    for element in data.get("elements", []):
        key = (element.get("type"), element.get("id"))
        if key[0] not in {'node', 'way', 'relation'} or not isinstance(key[1], int) or key[1] <= 0:
            continue
        if key in seen:
            continue
        seen.add(key)
        tags = element.get("tags", {})
        if not tags:
            continue
        position = element.get("center", element)
        objects.append({"osm_type": key[0], "osm_id": key[1], "category": classify(tags),
                        "name": tags.get("name", "Без названия"), "lat": position.get("lat"),
                        "lon": position.get("lon"), "tags": tags})
    counts = Counter(o["category"] for o in objects)
    return {"bbox": bbox, "area_km2": area, "source": source,
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "counts": {k: counts[k] for k in CATEGORIES}, "objects": objects}


def fetch_area(bbox, cache_dir):
    bbox, _ = validate_bbox(bbox)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    query = '[out:json][timeout:45];(nwr[building];nwr[amenity];nwr[shop];nwr[office];nwr[industrial];nwr[landuse];nwr[leisure];nwr[tourism];nwr[public_transport];nwr[railway~"^(station|halt|subway_entrance)$"];nwr[highway=bus_stop];);out center tags;'
    # Global bbox applies to every statement, preventing unbounded queries.
    query = query.replace('[timeout:45];', '[timeout:45][bbox:' + ','.join(map(str, bbox)) + '];')
    target = cache_dir / (hashlib.sha256(query.encode()).hexdigest() + '.json')
    if target.exists() and datetime.now().timestamp() - target.stat().st_mtime < 86400:
        return json.loads(target.read_text(encoding="utf-8"))
    response = requests.post('https://overpass-api.de/api/interpreter', data={"data": query},
                             headers={"User-Agent": "TrafficAI-SUMO-area-demo/1.0"}, timeout=65)
    response.raise_for_status()
    data = response.json()
    if data.get("remark"):
        raise ValueError("Overpass вернул неполный результат: " + data["remark"])
    target.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def report_html(summary):
    counts = summary['counts']
    total = len(summary['objects'])
    dominant = max(counts, key=counts.get) if total else None
    description = (f"Среди найденных объектов больше всего в категории «{CATEGORIES[dominant]}» ({counts[dominant]})."
                   if total else "В ответе источника нет объектов инфраструктуры.")
    composition = '; '.join(f'{label}: {counts[key]}' for key, label in CATEGORIES.items() if key != 'other' and counts[key])
    if composition: description += ' На карте отмечены: ' + composition + '.'
    rows = ''.join(f'<tr><td>{label}</td><td>{counts[key]}</td></tr>' for key, label in CATEGORIES.items())
    def source_link(obj):
        identifier = f'{obj["osm_type"]}/{obj["osm_id"]}'
        if summary['source'].startswith('Учебные'):
            return identifier + ' (учебный ID)'
        return f'<a href="https://www.openstreetmap.org/{identifier}">{identifier}</a>'
    objects = ''.join(f'<tr><td>{html.escape(o["name"])}</td><td>{CATEGORIES[o["category"]]}</td><td>{source_link(o)}</td></tr>' for o in summary['objects'])
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>Анализ района — TrafficAI</title>
    <style>body{{font:17px system-ui;max-width:1000px;margin:40px auto;padding:20px;color:#172338}}table{{border-collapse:collapse;width:100%;margin:20px 0}}td,th{{border:1px solid #ccd5df;padding:9px;text-align:left}}.note{{background:#eef3fa;padding:18px}}@media print{{body{{margin:0}}}}</style>
    <h1>Инфраструктура выбранной области</h1><p>Площадь: {summary['area_km2']:.2f} км². Найдено объектов OSM: {total}.</p>
    <p>{description}</p><p>Источник: {html.escape(summary['source'])}. Дата получения: {html.escape(summary['retrieved_at'])}.</p>
    <p>Границы (юг, запад, север, восток): {summary['bbox']}</p><h2>Состав инфраструктуры</h2>
    <table><tr><th>Категория</th><th>Объектов OSM</th></tr>{rows}</table>
    <p class="note">Это инвентаризация данных карты, а не измерение потоков или обеспеченности жителей. Здание и POI внутри него могут учитываться отдельно; остановки могут быть описаны несколькими объектами OSM. Отсутствие объекта в выгрузке не доказывает его отсутствие на местности. Плотность населения, посещаемость и транспортная нагрузка не оценивались. Текст сформирован по данным, без LLM.</p>
    <h2>Объекты и проверяемые источники</h2><table><tr><th>Название</th><th>Категория</th><th>OSM</th></tr>{objects}</table>
    <footer>© OpenStreetMap contributors — ODbL. <a href="https://www.openstreetmap.org/copyright">Условия использования</a></footer></html>'''


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
