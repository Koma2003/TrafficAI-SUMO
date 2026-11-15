#========================================================================
# Сравнение "до/после": прогоняет исходную и оптимизированную сеть,
# пишет текстовые отчёты о задержках и данных с петель.
#========================================================================

import os
import sys
import shutil
import tempfile
import math
import xml.etree.ElementTree as ET
from pathlib import Path
import traci

SCRIPT_DIR = Path(__file__).parent
SUMO_DIR = SCRIPT_DIR.parent

ORIGINAL_NET = SUMO_DIR / "test_intersection.net.xml"
OPTIMIZED_NET = SUMO_DIR / "test_intersection_optimized_ga.net.xml"
ROUTES_FILE = SUMO_DIR / "routes.rou.xml"
DETECTORS_FILE = SUMO_DIR / "detectors.add.xml"
CONFIG_FILE = SUMO_DIR / "intersection.sumocfg"

SIMULATION_DURATION = 360.0


class LaneStatistics:
    """Observed waiting seconds and stop transitions, independent of step size.

    A stop is a sampled transition from speed > 0.1 m/s to <= 0.1 m/s.
    The first sighting of a stationary vehicle is not a known transition.
    Per-vehicle means include every vehicle seen on the monitored lane,
    including unfinished journeys; arrivals are not the denominator.
    """
    def __init__(self, detector_ids, lane_to_detector):
        self.lane_to_detector = lane_to_detector
        self.total_delay = {detector: 0.0 for detector in detector_ids}
        self.stop_count = {detector: 0 for detector in detector_ids}
        self.unique_vehicles = {detector: set() for detector in detector_ids}
        self.queue_sum = {detector: 0.0 for detector in detector_ids}
        self.queue_seconds = {detector: 0.0 for detector in detector_ids}
        self.queue_max = {detector: 0 for detector in detector_ids}
        self.previous_wait = {}
        self.was_stopped = {}

    def record_vehicle(self, vehicle, lane, waiting, speed, seconds):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("Шаг наблюдения должен быть положительным числом секунд")
        previous = self.previous_wait.get(vehicle, 0.0)
        # getWaitingTime resets after movement. Count only time observed in
        # this step, never a new vehicle's entire historical waiting timer.
        increment = waiting - previous if waiting >= previous else waiting
        increment = max(0.0, min(seconds, increment))
        stopped = speed <= 0.1
        detector = self.lane_to_detector.get(lane)
        if detector is not None:
            self.unique_vehicles[detector].add(vehicle)
            self.total_delay[detector] += increment
            if stopped and self.was_stopped.get(vehicle) is False:
                self.stop_count[detector] += 1
        self.previous_wait[vehicle] = waiting
        self.was_stopped[vehicle] = stopped

    def record_queue(self, detector, halted, seconds):
        self.queue_sum[detector] += halted * seconds
        self.queue_seconds[detector] += seconds
        self.queue_max[detector] = max(self.queue_max[detector], halted)

    def averages(self, detector):
        vehicles = len(self.unique_vehicles[detector])
        observed_seconds = self.queue_seconds[detector]
        return {
            'delay_seconds_per_vehicle': self.total_delay[detector] / vehicles if vehicles else 0.0,
            'stops_per_vehicle': self.stop_count[detector] / vehicles if vehicles else 0.0,
            'mean_queue_vehicles': self.queue_sum[detector] / observed_seconds if observed_seconds else 0.0,
        }


def get_detector_ids():
    """Извлекает ID детекторов из файла."""
    if not DETECTORS_FILE.exists():
        print(f"Файл детекторов не найден: {DETECTORS_FILE}")
        return []
    tree = ET.parse(DETECTORS_FILE)
    root = tree.getroot()
    detectors = []
    detectors.extend([elem.get("id") for elem in root.findall("e1Detector")])
    detectors.extend([elem.get("id") for elem in root.findall("inductionLoop")])
    return detectors


def get_lane_from_detector(det_id):
    """Извлекает имя полосы из ID детектора."""
    if det_id.startswith("det_"):
        return det_id.replace("det_", "")
    if det_id.startswith("e1det_"):
        return det_id.replace("e1det_", "")
    return det_id


DETECTOR_IDS = get_detector_ids()
print(f"Загружено детекторов: {len(DETECTOR_IDS)}")
for d in DETECTOR_IDS:
    print(f"  - {d}")

LANE_TO_DETECTOR = {}
for det_id in DETECTOR_IDS:
    lane = get_lane_from_detector(det_id)
    LANE_TO_DETECTOR[lane] = det_id


def collect_statistics(net_path, output_prefix, label="Сеть"):
    """
    Запускает симуляцию и собирает ДВА типа данных:
      1. Задержки и очереди (через TraCI) -> {output_prefix}_delays.txt
      2. Данные с петель (пропускная способность) -> {output_prefix}_detectors.txt
    """
    print(f"\n{'=' * 60}")
    print(f" Сбор статистики для: {label}")
    print(f"   Сеть: {net_path.name}")
    print(f"{'=' * 60}")

    if not net_path.exists():
        print(f" Файл сети не найден: {net_path}")
        return False

    if not DETECTOR_IDS:
        print(" Нет детекторов для сбора данных")
        return False

    step_length = 1.0
    if CONFIG_FILE.exists():
        try:
            setting = ET.parse(CONFIG_FILE).getroot().find('time/step-length')
            if setting is not None:
                step_length = float(setting.get('value'))
            if not math.isfinite(step_length) or step_length <= 0:
                raise ValueError('Некорректный step-length')
        except (ET.ParseError, ValueError, TypeError, OSError) as exc:
            print(f" Не удалось прочитать шаг симуляции: {exc}")
            return False

    temp_net = Path(tempfile.mktemp(suffix=".net.xml"))
    shutil.copy(net_path, temp_net)

    temp_cfg = Path(tempfile.mktemp(suffix=".sumocfg"))
    cfg_content = f'''<?xml version="1.0" encoding="UTF-8"?>
<configuration>
    <input>
        <net-file value="{temp_net}"/>
        <route-files value="{ROUTES_FILE}"/>
        <additional-files value="{DETECTORS_FILE}"/>
    </input>
    <time>
        <begin value="0"/>
        <end value="{SIMULATION_DURATION:g}"/>
        <step-length value="{step_length:g}"/>
    </time>
    <processing>
        <time-to-teleport value="-1"/>
    </processing>
</configuration>'''
    with open(temp_cfg, "w", encoding="utf-8") as f:
        f.write(cfg_content)

    statistics = LaneStatistics(DETECTOR_IDS, LANE_TO_DETECTOR)
    total_delay = statistics.total_delay
    stop_count = statistics.stop_count
    queue_max = statistics.queue_max
    unique_vehicles = statistics.unique_vehicles
    all_vehicles = set()
    arrived_vehicles = 0

    detector_passed = {det_id: 0 for det_id in DETECTOR_IDS}
    detector_speed_sum = {det_id: 0.0 for det_id in DETECTOR_IDS}
    detector_speed_count = {det_id: 0 for det_id in DETECTOR_IDS}
    detector_occupancy_sum = {det_id: 0.0 for det_id in DETECTOR_IDS}
    detector_occupancy_count = {det_id: 0 for det_id in DETECTOR_IDS}

    last_active_time = 0
    sumo_cmd = ["sumo", "-c", str(temp_cfg), "--start", "--no-step-log"]

    try:
        traci.start(sumo_cmd)
        step = 0
        previous_time = traci.simulation.getTime()
        while previous_time < SIMULATION_DURATION - 1e-9:
            traci.simulationStep()
            step += 1
            current_time = traci.simulation.getTime()
            elapsed = current_time - previous_time
            if not math.isfinite(elapsed) or elapsed <= 0:
                raise ValueError('Время SUMO не продвигается')
            seconds = min(current_time, SIMULATION_DURATION) - previous_time
            previous_time = current_time
            arrived_vehicles += traci.simulation.getArrivedNumber()

            active_vehicles = traci.vehicle.getIDList()
            if active_vehicles:
                last_active_time = current_time

            for veh_id in active_vehicles:
                all_vehicles.add(veh_id)

            for veh_id in active_vehicles:
                current_wait = traci.vehicle.getWaitingTime(veh_id)
                try:
                    lane_id = traci.vehicle.getLaneID(veh_id)
                except:
                    lane_id = None
                statistics.record_vehicle(veh_id, lane_id, current_wait,
                                          traci.vehicle.getSpeed(veh_id), seconds)

            # Weight the queue by elapsed seconds, not by sample count.
            for det_id in DETECTOR_IDS:
                lane = get_lane_from_detector(det_id)
                try:
                    halted = traci.lane.getLastStepHaltingNumber(lane)
                    statistics.record_queue(det_id, halted, seconds)
                except:
                    pass

            for det_id in DETECTOR_IDS:
                try:
                    n_vehicles = traci.inductionloop.getLastStepVehicleNumber(det_id)
                    if n_vehicles > 0:
                        detector_passed[det_id] += n_vehicles
                        speed = traci.inductionloop.getLastStepMeanSpeed(det_id)
                        if speed >= 0:
                            detector_speed_sum[det_id] += speed
                            detector_speed_count[det_id] += 1
                        occupancy = traci.inductionloop.getLastStepOccupancy(det_id)
                        if occupancy >= 0:
                            detector_occupancy_sum[det_id] += occupancy
                            detector_occupancy_count[det_id] += 1
                except:
                    pass
            if traci.simulation.getMinExpectedNumber() == 0:
                break

        print(f" Симуляция завершена. Собрано {step} шагов.")

    except Exception as e:
        print(f" Ошибка во время симуляции: {e}")
        return False
    finally:
        try:
            traci.close()
        except:
            pass
        try:
            temp_net.unlink()
        except:
            pass
        try:
            temp_cfg.unlink()
        except:
            pass

    total_vehicles_seen = len(all_vehicles)

    all_detected_vehicles = set()
    for det_id in DETECTOR_IDS:
        all_detected_vehicles.update(unique_vehicles[det_id])
    total_detected_vehicles = len(all_detected_vehicles)

    delays_file = f"{output_prefix}_delays.txt"
    with open(delays_file, "w", encoding='utf-8') as f:
        f.write(f"ОТЧЁТ О ЗАДЕРЖКАХ И ОЧЕРЕДЯХ (ЧЕРЕЗ TraCI)\n")
        f.write(f"Сеть: {net_path.name}\n")
        f.write(f"Время сбора данных: до {previous_time:g} сек; шаг модели {step_length:g} с\n")
        f.write("Средние задержки и остановки рассчитаны по всем наблюдавшимся на полосе машинам, включая незавершившие поездку.\n")
        f.write("Остановка — наблюдавшийся переход скорости > 0.1 м/с к <= 0.1 м/с; начальная неподвижность не считается переходом.\n")
        f.write("=" * 80 + "\n\n")

        total_wait_all = 0.0
        total_stops_all = 0
        total_vehicles_all = 0

        for det_id in DETECTOR_IDS:
            lane = get_lane_from_detector(det_id)
            delay_sum = total_delay[det_id]
            averages = statistics.averages(det_id)
            avg_delay = averages['delay_seconds_per_vehicle']
            avg_queue = averages['mean_queue_vehicles']
            max_queue = queue_max[det_id]
            unique_cnt = len(unique_vehicles[det_id])
            stops_per_vehicle = averages['stops_per_vehicle']

            total_wait_all += delay_sum
            total_stops_all += stop_count[det_id]
            total_vehicles_all += unique_cnt

            f.write(f"Полоса '{lane}':\n")
            f.write(f"  - Суммарное время задержек: {delay_sum:.2f} сек\n")
            f.write(f"  - Количество наблюдавшихся остановок: {stop_count[det_id]}\n")
            f.write(f"  - СРЕДНЯЯ ЗАДЕРЖКА НА МАШИНУ: {avg_delay:.2f} сек\n")
            f.write(f"  - Количество уникальных машин: {unique_cnt}\n")
            f.write(f"  - Среднее количество остановок на машину: {stops_per_vehicle:.2f}\n")
            f.write(f"  - СРЕДНЯЯ ДЛИНА ОЧЕРЕДИ: {avg_queue:.2f}\n")
            f.write(f"  - МАКСИМАЛЬНАЯ ДЛИНА ОЧЕРЕДИ: {max_queue}\n\n")

        if DETECTOR_IDS:
            avg_wait_all = total_wait_all / len(DETECTOR_IDS) if DETECTOR_IDS else 0
            f.write("=" * 80 + "\n")
            f.write("СВОДКА ПО ВСЕМ ДЕТЕКТОРАМ:\n")
            f.write(f"  - Общее время задержек (сумма по полосам): {total_wait_all:.2f} сек\n")
            f.write(f"  - Среднее время задержек на детектор: {avg_wait_all:.2f} сек\n")
            f.write(f"  - Всего уникальных машин (сумма по полосам): {total_vehicles_all}\n")
            f.write(f"  - Всего уникальных машин (по всем детекторам): {total_detected_vehicles}\n")
            f.write(f"  - Всего машин, появившихся в симуляции: {total_vehicles_seen}\n")
            f.write(f"  - Прибывших автомобилей: {arrived_vehicles}\n")
            average_observed_delay = total_wait_all / total_detected_vehicles if total_detected_vehicles else 0.0
            average_observed_stops = total_stops_all / total_detected_vehicles if total_detected_vehicles else 0.0
            f.write(f"  - Средняя задержка на наблюдавшуюся машину: {average_observed_delay:.2f} сек\n")
            f.write(f"  - Среднее число остановок на наблюдавшуюся машину: {average_observed_stops:.2f}\n")

    print(f" Отчёт о задержках: {delays_file}")

    detectors_file = f"{output_prefix}_detectors.txt"
    with open(detectors_file, "w", encoding='utf-8') as f:
        f.write(f"ОТЧЁТ С ИНДУКЦИОННЫХ ПЕТЕЛЬ\n")
        f.write(f"Сеть: {net_path.name}\n")
        f.write(f"Время сбора данных: до {previous_time:g} сек; шаг модели {step_length:g} с\n")
        f.write("=" * 80 + "\n\n")

        for det_id in DETECTOR_IDS:
            lane = get_lane_from_detector(det_id)
            passed = detector_passed[det_id]
            avg_speed = detector_speed_sum[det_id] / detector_speed_count[det_id] if detector_speed_count[det_id] > 0 else 0.0
            avg_occupancy = detector_occupancy_sum[det_id] / detector_occupancy_count[det_id] if detector_occupancy_count[det_id] > 0 else 0.0

            f.write(f"Петля '{det_id}' (полоса '{lane}'):\n")
            f.write(f"  - Количество проехавших машин (по петле): {passed}\n")
            f.write(f"  - Средняя скорость: {avg_speed:.2f} м/с\n")
            f.write(f"  - Средняя занятость: {avg_occupancy:.2f}%\n\n")

        if DETECTOR_IDS:
            f.write("=" * 80 + "\n")
            f.write("СВОДКА ПО ВСЕМ ПЕТЛЯМ:\n")
            f.write(f"  - Всего проехало машин (по петлям, может быть завышено): {sum(detector_passed.values())}\n")
            avg_speed_all = sum(detector_speed_sum[d] / detector_speed_count[d] if detector_speed_count[d] > 0 else 0 for d in DETECTOR_IDS) / len(DETECTOR_IDS) if DETECTOR_IDS else 0
            avg_occupancy_all = sum(detector_occupancy_sum[d] / detector_occupancy_count[d] if detector_occupancy_count[d] > 0 else 0 for d in DETECTOR_IDS) / len(DETECTOR_IDS) if DETECTOR_IDS else 0
            f.write(f"  - Средняя скорость по всем петлям: {avg_speed_all:.2f} м/с\n")
            f.write(f"  - Средняя занятость по всем петлям: {avg_occupancy_all:.2f}%\n")

    print(f" Отчёт с петель: {detectors_file}")

    return True


def main():
    print("=" * 60)
    print("УНИВЕРСАЛЬНОЕ СРАВНЕНИЕ СЕТЕЙ (ДВА ТИПА ДАННЫХ)")
    print("=" * 60)

    for f in [ORIGINAL_NET, OPTIMIZED_NET, ROUTES_FILE, DETECTORS_FILE]:
        if not f.exists():
            print(f" Файл не найден: {f}")
            return

    print(f"\n Исходная сеть: {ORIGINAL_NET.name}")
    print(f" Оптимизированная сеть: {OPTIMIZED_NET.name}")
    print(f" Детекторов: {len(DETECTOR_IDS)}")

    collect_statistics(ORIGINAL_NET, str(SCRIPT_DIR / "original"), label="Исходная сеть")
    collect_statistics(OPTIMIZED_NET, str(SCRIPT_DIR / "optimized"), label="Оптимизированная сеть")

    print("\n" + "=" * 60)
    print(" Сравнение завершено!")
    print(f"   Файлы для исходной сети: original_delays.txt, original_detectors.txt")
    print(f"   Файлы для оптимизированной сети: optimized_delays.txt, optimized_detectors.txt")
    print("=" * 60)


if __name__ == "__main__":
    main()
