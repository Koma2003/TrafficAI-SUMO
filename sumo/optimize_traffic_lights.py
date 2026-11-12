#========================================================================
# ГЕНЕТИЧЕСКИЙ АЛГОРИТМ: подбор длительностей зелёных фаз светофора.
#========================================================================

# Параметры диапазонов, поколений и seed задаются через CLI.
import argparse
import sys
import random
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
import traci

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
from sumo.phase_bounds import bounded_durations

NET_FILE = SCRIPT_DIR / "test_intersection.net.xml"
ROUTES_FILE = SCRIPT_DIR / "routes.rou.xml"
DETECTORS_FILE = SCRIPT_DIR / "detectors.add.xml"
CONFIG_FILE = SCRIPT_DIR / "intersection.sumocfg"

SIMULATION_DURATION = 360
SIMULATION_SEED = 42
POPULATION_SIZE = 15
GENERATIONS = 5
ELITE_SIZE = 4
MUTATION_STRENGTH =  12
MIN_DUR = 15
MAX_DUR = 90


def get_detector_ids():
    if not DETECTORS_FILE.exists():
        return []
    tree = ET.parse(DETECTORS_FILE)
    root = tree.getroot()
    return [elem.get("id") for elem in root.findall("e1Detector")]


DETECTOR_IDS = get_detector_ids()
print(f"Загружено детекторов: {len(DETECTOR_IDS)}")
for d in DETECTOR_IDS:
    print(f"  - {d}")


def get_phase_info(net_path):
    """
    Автоматически определяет оптимизируемые фазы.

    Оптимизируются ТОЛЬКО автомобильные фазы:
      - не содержит 'y' (не жёлтая)
      - содержит заглавную 'G' (автомобильный зелёный)

    Пешеходные фазы (содержат только 'g' без 'G') и жёлтые ('y')
    остаются фиксированными.
    """
    tree = ET.parse(net_path)
    tl = tree.getroot().find("tlLogic")
    if tl is None:
        raise ValueError("В сети не найден tlLogic")

    phases = []
    for phase in tl.findall("phase"):
        state = phase.get("state", "")
        duration = float(phase.get("duration", 0))
        phases.append({
            "state": state,
            "duration": duration,
            "is_yellow": 'y' in state.lower(),
            "has_car_green": 'G' in state,
        })

    optimize_indices = [
        i for i, p in enumerate(phases)
        if not p["is_yellow"] and p["has_car_green"]
    ]

    fixed_durations = {i: p["duration"] for i, p in enumerate(phases) if i not in optimize_indices}

    total_cycle_duration = sum(p["duration"] for p in phases)
    fixed_total = sum(fixed_durations.values())
    available_for_optimization = total_cycle_duration - fixed_total

    return phases, optimize_indices, fixed_durations, total_cycle_duration, available_for_optimization


def get_current_green_durations(net_path, optimize_indices):
    phases, _, _, _, _ = get_phase_info(net_path)
    return [phases[i]["duration"] for i in optimize_indices]


def set_phase_durations(net_path, green_durations, optimize_indices, fixed_durations):
    tree = ET.parse(net_path)
    tl = tree.getroot().find("tlLogic")
    all_phases = tl.findall("phase")
    for idx, new_dur in zip(optimize_indices, green_durations):
        if idx < len(all_phases):
            all_phases[idx].set("duration", str(new_dur))
    for idx, fixed_dur in fixed_durations.items():
        if idx < len(all_phases):
            all_phases[idx].set("duration", str(fixed_dur))
    tree.write(net_path, encoding="utf-8", xml_declaration=True)


# Фитнес-функция. БАГ: считает задержку только машин, УЖЕ находящихся в сети
# (traci.vehicle.getIDList()). Машина, не сумевшая въехать, не учитывается ->
# алгоритму выгодно "морить" направление: меньше машин = меньше задержка.
def evaluate(green_durations, optimize_indices, fixed_durations, work_dir):
    """
    Оценивает особь: запускает симуляцию и возвращает общую задержку (суммарное время ожидания всех машин).
    """
    temp_net = work_dir / "temp.net.xml"
    shutil.copy(NET_FILE, temp_net)
    set_phase_durations(temp_net, green_durations, optimize_indices, fixed_durations)

    sumo_cmd = [
        "sumo", "-c", str(CONFIG_FILE),
        "--net-file", str(temp_net),
        "--additional-files", str(DETECTORS_FILE),
        "--time-to-teleport", "-1",
        "--seed", str(SIMULATION_SEED),
        "--start", "--no-step-log"
    ]

    total_waiting = 0.0
    step = 0
    prev_wait = {}

    try:
        traci.start(sumo_cmd)
        while step < SIMULATION_DURATION and traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            step += 1

            veh_list = traci.vehicle.getIDList()
            for veh_id in veh_list:
                current_wait = traci.vehicle.getWaitingTime(veh_id)
                if veh_id not in prev_wait:
                    prev_wait[veh_id] = current_wait
                else:
                    delta = current_wait - prev_wait[veh_id]
                    if delta > 0:
                        total_waiting += delta
                    prev_wait[veh_id] = current_wait

    finally:
        try:
            traci.close()
        except:
            pass

    return total_waiting


def create_individual(base_green, available_for_optimization, optimize_indices, fixed_durations):
    return bounded_durations([random.uniform(MIN_DUR, MAX_DUR) for _ in base_green],
                             available_for_optimization, MIN_DUR, MAX_DUR)


def mutate(ind, available_for_optimization):
    return bounded_durations([v + random.uniform(-MUTATION_STRENGTH, MUTATION_STRENGTH) for v in ind],
                             available_for_optimization, MIN_DUR, MAX_DUR)


def crossover(p1, p2, available_for_optimization):
    return bounded_durations([a if random.random() < .5 else b for a, b in zip(p1, p2)],
                             available_for_optimization, MIN_DUR, MAX_DUR)


def main():
    global MIN_DUR, MAX_DUR, GENERATIONS, SIMULATION_SEED
    parser = argparse.ArgumentParser(description="Генетический подбор зелёных фаз в заданных пределах")
    parser.add_argument('--green-min', type=float, default=15)
    parser.add_argument('--green-max', type=float, default=90)
    parser.add_argument('--generations', type=int, default=5)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    MIN_DUR, MAX_DUR, GENERATIONS = args.green_min, args.green_max, args.generations
    if GENERATIONS < 1: parser.error('Нужно минимум одно поколение')
    SIMULATION_SEED = args.seed
    random.seed(args.seed)
    print("=" * 60)
    print("ГЕНЕТИЧЕСКАЯ ОПТИМИЗАЦИЯ (СОХРАНЕНИЕ ДЛИНЫ ЦИКЛА)")
    print("=" * 60)

    for f in [NET_FILE, ROUTES_FILE, DETECTORS_FILE, CONFIG_FILE]:
        if not f.exists():
            print(f" Файл не найден: {f}")
            return

    phases, optimize_indices, fixed_durations, total_cycle, available_for_optimization = get_phase_info(NET_FILE)

    print(f"\n Анализ светофора:")
    print(f"  Всего фаз: {len(phases)}")
    print(f"  Общая длительность цикла: {total_cycle:.1f} сек")
    print(f"  Оптимизируемые фазы: {optimize_indices}")
    for i in optimize_indices:
        print(f"    Фаза {i}: state='{phases[i]['state']}', длит={phases[i]['duration']:.1f} сек")
    print(f"  Фиксированные фазы: {list(fixed_durations.keys())}")
    print(f"  Доступно для оптимизации: {available_for_optimization:.1f} сек")

    base_green = get_current_green_durations(NET_FILE, optimize_indices)
    try:
        feasible_base = bounded_durations(base_green, available_for_optimization, MIN_DUR, MAX_DUR)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"\n Оптимизируем {len(base_green)} фаз")
    print(f"  Диапазон: {MIN_DUR}..{MAX_DUR} сек")
    print(f"  Сумма оптимизируемых фаз должна быть: {available_for_optimization:.1f} сек")

    print("\n[Оценка исходного цикла]")
    with tempfile.TemporaryDirectory() as tmpdir:
        base_fitness = evaluate(base_green, optimize_indices, fixed_durations, Path(tmpdir))
    print(f"  Fitness (общая задержка, сек): {base_fitness:.2f}")

    population = [
        create_individual(base_green, available_for_optimization, optimize_indices, fixed_durations)
        for _ in range(POPULATION_SIZE)
    ]
    # The baseline may violate user bounds: never export it as a valid candidate.
    with tempfile.TemporaryDirectory() as tmpdir:
        best_fitness = evaluate(feasible_base, optimize_indices, fixed_durations, Path(tmpdir))
    best_individual = feasible_base.copy()

    for gen in range(GENERATIONS):
        if gen > 0:
            population[0] = best_individual.copy()

        fitnesses = []
        with tempfile.TemporaryDirectory() as tmpdir:
            work_dir = Path(tmpdir)
            for idx, ind in enumerate(population):
                fit = evaluate(ind, optimize_indices, fixed_durations, work_dir)
                fitnesses.append(fit)
                if fit < best_fitness:
                    best_fitness = fit
                    best_individual = ind.copy()
                    improvement = ((best_fitness - base_fitness) / base_fitness * 100 if base_fitness else 0.0)
                    print(f"★ Gen {gen + 1}/{GENERATIONS} Ind {idx + 1}: {fit:.2f} (улучшение {improvement:+.1f}%)")
                else:
                    print(f"  Gen {gen + 1}/{GENERATIONS} Ind {idx + 1}: {fit:.2f}")

        sorted_idx = sorted(range(POPULATION_SIZE), key=lambda i: fitnesses[i])
        sorted_pop = [population[i] for i in sorted_idx]
        new_pop = [sorted_pop[i].copy() for i in range(min(ELITE_SIZE, len(sorted_pop)))]
        while len(new_pop) < POPULATION_SIZE:
            p1 = random.choice(sorted_pop[:min(ELITE_SIZE * 2, len(sorted_pop))])
            p2 = random.choice(sorted_pop[:min(ELITE_SIZE * 2, len(sorted_pop))])
            child = crossover(p1, p2, available_for_optimization)
            child = mutate(child, available_for_optimization)
            new_pop.append(child)
        population = new_pop
        print(f"  --> Gen {gen + 1}: лучшее = {best_fitness:.2f}\n")

    print("\n[РЕЗУЛЬТАТ]")
    print(f"Исходный fitness: {base_fitness:.2f}")
    print(f"Оптимизированный: {best_fitness:.2f}")
    improvement = ((best_fitness - base_fitness) / base_fitness * 100 if base_fitness else 0.0)
    print(f"Улучшение: {improvement:+.1f}%")
    print(f"\n Оптимальные длительности фаз:")
    print(f"  Общая длина цикла: {sum(best_individual) + sum(fixed_durations.values()):.1f} сек")
    for i, idx in enumerate(optimize_indices):
        print(f"  Фаза {idx}: {best_individual[i]:.1f} сек (было {base_green[i]:.1f} сек)")

    output_net = SCRIPT_DIR / "test_intersection_optimized_ga.net.xml"
    shutil.copy(NET_FILE, output_net)
    set_phase_durations(output_net, best_individual, optimize_indices, fixed_durations)

    output_cfg = SCRIPT_DIR / "test_intersection_optimized_ga.sumo.cfg"
    # Preserve the same routes (including pedestrians) and processing settings.
    config_tree = ET.parse(CONFIG_FILE)
    config_tree.getroot().find("input/net-file").set("value", output_net.name)
    config_tree.write(output_cfg, encoding="utf-8", xml_declaration=True)

    print(f"\n Оптимизированная сеть: {output_net}")
    print(f" Конфиг для запуска: {output_cfg}")
    print(f"\n Запуск: sumo-gui -c {output_cfg.name}")


if __name__ == "__main__":
    main()