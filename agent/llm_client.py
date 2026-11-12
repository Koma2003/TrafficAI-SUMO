#========================================================================
# ГЛАВНЫЙ ФАЙЛ ПРОЕКТА — ОРКЕСТРАТОР КОНВЕЙЕРА.
# По очереди запускает: карту -> построение geometry.json -> сеть -> маршруты.
# Шаг "LLM" заменён детерминированным osm/geometry_builder.py.
#========================================================================

import io
import sys
import json
import subprocess
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

PROJECT_ROOT = Path(__file__).parent.parent

COORDS_FILE = PROJECT_ROOT / "clicked_coords.json"
OUTPUT_FILE = PROJECT_ROOT / "osm" / "geometry.json"
NETWORK_FILE = PROJECT_ROOT / "sumo" / "test_intersection.net.xml"
ROUTES_FILE = PROJECT_ROOT / "sumo" / "routes.rou.xml"
CONFIG_FILE = PROJECT_ROOT / "sumo" / "intersection.sumocfg"


def run_intersection_finder():
    """Запускает intersection_finder, получает координаты."""
    result = subprocess.run(
        ["python", str(PROJECT_ROOT / "osm" / "intersection_finder.py")],
        cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError("intersection_finder завершился с ошибкой")
    if not COORDS_FILE.exists():
        raise RuntimeError("Файл с координатами не создан")
    with open(COORDS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def run_geometry_builder():
    """Запускает osm/geometry_builder.py (детерминированный парсер OSM)."""
    print("\n" + "=" * 50)
    print("ШАГ 2: ПОСТРОЕНИЕ ГЕОМЕТРИИ (OSM -> geometry.json)")
    print("=" * 50)

    result = subprocess.run(
        ["python", str(PROJECT_ROOT / "osm" / "geometry_builder.py")],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        print(result.stdout)
        print(f"STDERR: {result.stderr}")
        raise RuntimeError("geometry_builder завершился с ошибкой")

    print(result.stdout)

    if not OUTPUT_FILE.exists():
        raise RuntimeError(f"Файл {OUTPUT_FILE} не создан")

    with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
        approaches = json.load(f)
    print(f"✅ geometry.json: {len(approaches)} подходов")
    return approaches


def build_network():
    """Запускает net_builder для создания SUMO сети."""
    print("\n" + "=" * 50)
    print("ШАГ 3: ПОСТРОЕНИЕ SUMO СЕТИ")
    print("=" * 50)

    result = subprocess.run(
        ["python", str(PROJECT_ROOT / "sumo" / "net_builder.py")],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        print(f"STDERR: {result.stderr}")
        raise RuntimeError("net_builder завершился с ошибкой")

    print(result.stdout)
    print(f"✅ Сеть сохранена в {NETWORK_FILE}")


def build_routes():
    """Запускает route_builder для создания маршрутов и детекторов."""
    print("\n" + "=" * 50)
    print("ШАГ 4: ГЕНЕРАЦИЯ МАРШРУТОВ И ДЕТЕКТОРОВ")
    print("=" * 50)

    result = subprocess.run(
        ["python", str(PROJECT_ROOT / "sumo" / "route_builder.py")],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode != 0:
        print(f"STDERR: {result.stderr}")
        raise RuntimeError("route_builder завершился с ошибкой")

    print(result.stdout)
    print(f"✅ Маршруты сохранены в {ROUTES_FILE}")
    print(f"✅ Конфиг сохранён в {CONFIG_FILE}")


def main():
    try:
        run_intersection_finder()
        run_geometry_builder()
        build_network()
        build_routes()

        print("\n" + "=" * 60)
        print("ВСЕ ШАГИ ВЫПОЛНЕНЫ УСПЕШНО!")
        print(f"   Файл сети: {NETWORK_FILE}")
        print(f"   Файл маршрутов: {ROUTES_FILE}")
        print(f"   Файл конфига: {CONFIG_FILE}")
        print("=" * 60)
        return 0

    except Exception as e:
        print(f"\n❌ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())