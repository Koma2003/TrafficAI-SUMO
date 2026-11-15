"""Constrained static signal plans on a user's SUMO model (GA, not RL).

Every continuous vehicle green/yellow/red interval is constrained, including
intervals wrapping across the cycle boundary. Pedestrian and clearance minima
are inherited from the input program. All artifacts go into a separate run.
"""
import argparse
import hashlib
import html
import json
import math
import random
import shutil
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

_SIMULATION_LOCK = threading.Lock()


def _net_path(config):
    config = Path(config).resolve()
    root = ET.parse(config).getroot()
    item = root.find('input/net-file')
    if item is None or not item.get('value'):
        raise ValueError('В конфигурации нет input/net-file')
    return (config.parent / item.get('value')).resolve()


def _ped_indices(root, tls_id):
    ped_edges = {e.get('id') for e in root.findall('edge') if e.get('function') in {'crossing', 'walkingarea'}}
    return {int(c.get('linkIndex')) for c in root.findall('connection')
            if c.get('tl') == tls_id and c.get('linkIndex', '-1') != '-1'
            and (c.get('from') in ped_edges or c.get('to') in ped_edges)}


def _kind(state, pedestrian):
    car = [s for i, s in enumerate(state) if i not in pedestrian]
    if any(s.lower() == 'y' for s in car): return 'yellow'
    if any(s in 'Gg' for s in car): return 'green'
    if any(state[i] in 'Gg' for i in pedestrian): return 'pedestrian'
    if all(s in 'rR' for s in state): return 'all_red'
    return 'fixed'


def inspect_model(config):
    net = _net_path(config)
    root = ET.parse(net).getroot()
    programs = []
    for tl in root.findall('tlLogic'):
        ped = _ped_indices(root, tl.get('id'))
        phases = [{'state': p.get('state'), 'duration': float(p.get('duration')),
                   'kind': _kind(p.get('state'), ped)} for p in tl.findall('phase')]
        programs.append({'id': tl.get('id'), 'program_id': tl.get('programID', '0'),
                         'cycle': sum(p['duration'] for p in phases), 'phases': phases})
    return {'net_file': str(net), 'has_signals': bool(programs), 'traffic_lights': programs}


def _stage_scenario(config, output, network=None):
    """Copy inputs; detector outputs and settings are isolated from the source."""
    config, output = Path(config).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    tree = ET.parse(config)
    root = tree.getroot()
    net = _net_path(config) if network is None else Path(network).resolve()
    if net != output / 'network.net.xml': shutil.copyfile(net, output / 'network.net.xml')
    root.find('input/net-file').set('value', 'network.net.xml')
    for tag in ['route-files', 'additional-files', 'weight-files']:
        entry = root.find('input/' + tag)
        if entry is None: continue
        names = []
        for i, value in enumerate(filter(None, entry.get('value', '').split(','))):
            source = (config.parent / value).resolve()
            dest = output / f'{tag}_{i}_{source.name}'
            shutil.copyfile(source, dest)
            document=ET.parse(dest)
            if any(node.tag == 'include' or node.get('href') for node in document.getroot().iter()):
                raise ValueError('Входные XML с внешними include/href пока не поддерживаются: '+source.name)
            if tag == 'additional-files':
                extra = document
                if extra.getroot().find('tlLogic') is not None:
                    raise ValueError('Программа светофора в additional-файле: сначала объедините её с сетью')
                for j, node in enumerate(extra.getroot().iter()):
                    if node.get('file'):
                        if node.tag not in {'e1Detector','inductionLoop','e2Detector','laneAreaDetector','edgeData','laneData','routeProbe'}:
                            raise ValueError('Внешний file у элемента '+node.tag+' пока не поддерживается: '+source.name)
                        node.set('file', f'detector_{i}_{j}.xml')
                    if node.get('output'):
                        if node.tag != 'calibrator':
                            raise ValueError('Неподдерживаемый output у элемента '+node.tag)
                        node.set('output', f'calibrator_{i}_{j}.xml')
                extra.write(dest, encoding='utf-8', xml_declaration=True)
            names.append(dest.name)
        entry.set('value', ','.join(names))
    settings = root.find('gui_only/gui-settings-file')
    if settings is not None:
        source = (config.parent / settings.get('value')).resolve()
        shutil.copyfile(source, output / 'gui-settings.xml')
        settings.set('value', 'gui-settings.xml')
    # Avoid writing output paths belonging to the source scenario.
    outputs = root.find('output')
    if outputs is not None: root.remove(outputs)
    target = output / 'scenario.sumocfg'
    tree.write(target, encoding='utf-8', xml_declaration=True)
    return target


def prepare_controlled_model(config, output_dir):
    """Explicit simulation variant of the current roads; never edit source net."""
    source = _net_path(config)
    if inspect_model(config)['has_signals']:
        return _stage_scenario(config, output_dir)
    root = ET.parse(source).getroot()
    centers = [j.get('id') for j in root.findall('junction') if j.get('id') == 'center']
    if len(centers) != 1:
        raise ValueError('Автоподготовка поддерживает один узел center. Для другой сети задайте регулируемый SUMO-сценарий.')
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / 'controlled.net.xml'
    result = subprocess.run(['netconvert', '--sumo-net-file', str(source), '--tls.set', 'center',
                             '--tls.default-type', 'static', '--tls.green.time', '30', '--tls.yellow.time', '3',
                             '--tls.allred.time', '1', '--output-file', str(target)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    if result.returncode:
        raise ValueError('Не удалось создать регулируемый вариант: ' + result.stderr)
    prepared = _stage_scenario(config, output, target)
    (output / 'variant.json').write_text(json.dumps({'source_config': str(Path(config).resolve()),
        'source_network_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'description': 'Явно созданный регулируемый вариант модели; не утверждение о светофорах на местности.',
        'created_at': datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2), encoding='utf-8')
    return prepared


def circular_runs(states, link, color):
    """Phase-index runs of one signal color; merge wraparound intervals."""
    match = lambda state: state[link] in {'green': 'Gg', 'yellow': 'yY', 'red': 'rR'}[color]
    flags = [match(s) for s in states]
    if not any(flags): return []
    if all(flags): return [list(range(len(states)))]
    starts = [i for i, flag in enumerate(flags) if flag and not flags[(i - 1) % len(flags)]]
    runs = []
    for start in starts:
        run, i = [], start
        while flags[i]:
            run.append(i)
            i = (i + 1) % len(flags)
        runs.append(run)
    return runs


def _solve_plan(problem, objective, target=None):
    """Integer step counts: exported intervals must be realizable by SUMO."""
    from scipy.optimize import linprog
    n, step = len(problem['states']), problem['step']
    extra = n if target is not None else 0
    rows = [[v*step for v in row] + [0]*extra for row in problem['a']]
    right = list(problem['b'])
    bounds = [(math.ceil(lo/step-1e-8), math.floor(hi/step+1e-8)) for lo,hi in problem['bounds']]
    if any(lo>hi for lo,hi in bounds): return None
    if target is not None:
        for i,value in enumerate(target):
            row=[0.]*(2*n); row[i]=step; row[n+i]=-1
            rows.append(row); right.append(value)
            row=[0.]*(2*n); row[i]=-step; row[n+i]=-1
            rows.append(row); right.append(-value)
        bounds += [(0,None)]*n
    kwargs=dict(A_ub=rows or None,b_ub=right or None,A_eq=[[step]*n+[0]*extra],b_eq=[problem['cycle']],
                bounds=bounds,integrality=[1]*n+[0]*extra,method='highs')
    costs=list(objective)+([1.]*n if target is not None else [])
    result=linprog(costs,**kwargs)
    if result.status == 4:
        result=linprog(costs,**kwargs,options={'presolve':False})
    return [round(v)*step for v in result.x[:n]] if result.success else None


def _problem(net, limits, cycle, step=1.):
    tree = ET.parse(net)
    root = tree.getroot()
    programs = root.findall('tlLogic')
    if len(programs) != 1:
        raise ValueError('Текущая настройка поддерживает одну программу одного светофора. Выберите подходящую модель.')
    tl = programs[0]
    if tl.get('type') != 'static':
        raise ValueError('Для этого оптимизатора нужна статическая программа светофора')
    phases = tl.findall('phase')
    if any(p.get('next') for p in phases):
        raise ValueError('Ветвящиеся программы фаз пока не поддерживаются')
    states = [p.get('state', '') for p in phases]
    if not states or not states[0] or len({len(s) for s in states}) != 1:
        raise ValueError('Некорректная длина состояний светофора')
    if any(set(s) - set('rRyYGg') for s in states):
        raise ValueError('В программе есть неподдерживаемые сигнальные состояния')
    ped = _ped_indices(root, tl.get('id'))
    car_links = sorted({int(c.get('linkIndex')) for c in root.findall('connection')
                       if c.get('tl') == tl.get('id') and c.get('linkIndex', '-1') != '-1'} - ped)
    if not car_links: raise ValueError('Не найдены управляемые автомобильные направления')
    baseline = [float(p.get('duration')) for p in phases]
    if not all(math.isfinite(d) and d > 0 for d in baseline): raise ValueError('Длительности фаз должны быть положительными')
    target_cycle = sum(baseline) if cycle is None else float(cycle)
    if not math.isfinite(target_cycle) or target_cycle <= 0: raise ValueError('Цикл должен быть положительным числом секунд')
    if not math.isfinite(step) or step < .001: raise ValueError('Некорректный шаг времени SUMO')
    if abs(target_cycle/step-round(target_cycle/step))>1e-6:
        raise ValueError('Цикл должен быть кратен шагу времени SUMO')
    bounds, kinds = [], []
    for phase_index, (state, original) in enumerate(zip(states, baseline)):
        kind = _kind(state, ped)
        kinds.append(kind)
        if kind in {'pedestrian', 'fixed'}:
            lower = upper = original
        elif kind == 'all_red':
            lower, upper = original, limits['all_red'][1]
        elif kind == 'yellow':
            # A continuous yellow may also consist of several subdivisions.
            lower, upper = original, limits['yellow'][1]
        else:
            # Two adjacent phases may be one continuous vehicle green (e.g.
            # pedestrian walk followed by clearance). Constrain the whole run,
            # not each subdivision as if it were a new 15-second green.
            lower, upper = step, limits['green'][1]
        if any(state[i] in 'Gg' for i in ped): lower = max(lower, original)
        previous = states[(phase_index - 1) % len(states)]
        if any(previous[i] in 'Gg' and state[i] not in 'Gg' for i in ped):
            lower = max(lower, original)
        if lower > upper:
            raise ValueError(f'Фаза {len(bounds)}: исходный защитный/пешеходный минимум {lower:g} с превышает максимум {upper:g} с')
        bounds.append((lower, upper))
    rows, right, runs = [], [], []
    for link in car_links:
        if not any(s[link] in 'Gg' for s in states):
            raise ValueError(f'Направление {link} никогда не получает зелёный в исходном плане')
        if all(s[link] in 'Gg' for s in states) or not any(s[link] in 'rR' for s in states):
            raise ValueError(f'Направление {link} не имеет красного интервала: этот план не поддерживает заданные ограничения цветов')
        if limits['yellow'][0] > 0 and not any(s[link] in 'yY' for s in states):
            raise ValueError(f'У направления {link} в исходной программе нет жёлтого интервала, '
                             'но задан положительный минимум жёлтого. Выберите программу с жёлтым переходом: '
                             'оптимизатор не добавляет фазы и не меняет их порядок.')
        for color in ['green', 'yellow', 'red']:
            for run in circular_runs(states, link, color):
                row = [float(i in run) for i in range(len(states))]
                lower, upper = limits[color]
                # Preserve original clearance time for every yellow run.
                if color == 'yellow': lower = max(lower, sum(baseline[i] for i in run))
                rows.extend([row, [-v for v in row]])
                right.extend([upper, -lower])
                runs.append({'link': link, 'color': color, 'indices': run, 'min': lower, 'max': upper})
    all_red_states=['r' if all(s in 'rR' for s in state) else 'G' for state in states]
    all_red_runs = circular_runs(all_red_states,0,'red')
    if limits['all_red'][0] > 0 and not all_red_runs:
        raise ValueError('В исходной программе нет фазы «всем красный», но задан положительный минимум. '
                         'Укажите минимум «всем красный» 0 с либо выберите программу с такой фазой. '
                         'Оптимизатор не добавляет фазы и не меняет их порядок.')
    for run in all_red_runs:
        row=[float(i in run) for i in range(len(states))]
        lower=max(limits['all_red'][0],sum(baseline[i] for i in run))
        upper=limits['all_red'][1]
        rows.extend([row,[-v for v in row]]);right.extend([upper,-lower])
        runs.append({'link':'все','color':'all_red','indices':run,'min':lower,'max':upper})
    problem = {'tree': tree, 'tl': tl, 'states': states, 'baseline': baseline, 'bounds': bounds,
            'a': rows, 'b': right, 'runs': runs, 'kinds': kinds,
            'cycle': target_cycle, 'car_links': car_links, 'step': step}
    feasible = _solve_plan(problem,[0.]*len(states))
    if feasible is None:
        raise ValueError(f'Диапазоны несовместимы с циклом {target_cycle:g} с и исходными пешеходными/защитными фазами. '
                         'Увеличьте максимум красного, измените цикл или диапазон зелёного. Ни один недопустимый план не сохранён.')
    problem['feasible'] = feasible
    return problem


def validate_durations(problem, durations):
    if len(durations) != len(problem['bounds']) or not all(math.isfinite(v) for v in durations): return False
    if abs(sum(durations) - problem['cycle']) > 1e-6: return False
    if any(abs(v/problem['step']-round(v/problem['step']))>1e-6 for v in durations): return False
    if any(not lo - 1e-6 <= value <= hi + 1e-6 for value, (lo, hi) in zip(durations, problem['bounds'])): return False
    return all(run['min'] - 1e-6 <= sum(durations[i] for i in run['indices']) <= run['max'] + 1e-6 for run in problem['runs'])


def _write_network(problem, durations, target):
    for phase, duration in zip(problem['tl'].findall('phase'), durations):
        phase.set('duration', format(duration, '.12g'))
    problem['tree'].write(target, encoding='utf-8', xml_declaration=True)


def _engine(backend):
    if backend in {'auto', 'libsumo'}:
        try:
            import libsumo
            return libsumo, 'libsumo'
        except ImportError:
            if backend == 'libsumo': raise ValueError('libsumo не импортируется: установите совместимую сборку или выберите traci')
    import traci
    return traci, 'traci'


def _evaluate(config, engine, seed, cancel):
    root = ET.parse(config).getroot()
    begin_entry, end_entry = root.find('time/begin'), root.find('time/end')
    begin = float(begin_entry.get('value')) if begin_entry is not None else 0
    end = float(end_entry.get('value')) if end_entry is not None else begin + 360
    if end <= begin: raise ValueError('Конец симуляции должен быть позже начала')
    engine.start(['sumo', '-c', str(config), '--seed', str(seed), '--no-step-log', 'true',
                  '--duration-log.disable', 'true', '--time-to-teleport', '-1'])
    cost, arrived, peak, active = 0., 0, 0, 0
    try:
        version = engine.getVersion()[1]
        while engine.simulation.getTime() < end - 1e-9 and engine.simulation.getMinExpectedNumber() > 0:
            if cancel is not None and cancel.is_set(): raise InterruptedError('Расчёт отменён')
            before = engine.simulation.getTime()
            engine.simulationStep()
            dt = min(engine.simulation.getTime(), end) - before
            stopped = sum(engine.vehicle.getSpeed(v) < .1 for v in engine.vehicle.getIDList())
            pending = len(engine.simulation.getPendingVehicles())
            cost += (stopped + pending) * dt
            peak = max(peak, stopped)
            active = engine.vehicle.getIDCount()
            arrived += engine.simulation.getArrivedNumber()
        return {'waiting_vehicle_seconds': cost, 'arrived': arrived, 'peak_stopped': peak,
                'unfinished_vehicles': active + len(engine.simulation.getPendingVehicles()), 'sumo_version': version}
    finally:
        engine.close()


def _report(result):
    before, after = result['baseline'], result['optimized']
    metrics = ''.join(f'<tr><td>{label}</td><td>{before[key]:.2f}</td><td>{after[key]:.2f}</td></tr>' for key, label in [
        ('waiting_vehicle_seconds', 'Остановившиеся и ожидающие въезда, авто·с'), ('arrived', 'Прибывшие автомобили'),
        ('peak_stopped', 'Максимум остановившихся автомобилей'), ('unfinished_vehicles', 'Незавершённые поездки к концу прогона')])
    phases = ''.join(f'<tr><td>{i}</td><td><code>{html.escape(p["state"])}</code></td><td>{p["kind"]}</td><td>{p["before"]:.2f}</td><td>{p["after"]:.2f}</td></tr>' for i,p in enumerate(result['phases']))
    ranges = ''.join(f'<tr><td>{r["link"]}</td><td>{r["color"]}</td><td>{r["duration"]:.2f}</td><td>{r["min"]:g}–{r["max"]:g}</td><td>Да</td></tr>' for r in result['checked_intervals'])
    return f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>TrafficAI — ограничения светофора</title>
    <style>body{{font:17px system-ui;max-width:1100px;margin:35px auto;padding:15px;color:#15263d}}table{{border-collapse:collapse;width:100%;margin:20px 0}}td,th{{border:1px solid #cbd5e1;padding:9px;text-align:left}}code{{overflow-wrap:anywhere}}.ok{{background:#e4f6ed;padding:18px}}</style>
    <h1>Светофорный план текущей модели</h1><p>Источник: {html.escape(result['source_config'])}</p>
    <p>Цикл: {result['cycle']:.2f} с. Метод: генетический поиск среди допустимых планов. RL в этом расчёте не используется.</p>
    <p class="ok">Все непрерывные автомобильные зелёные, жёлтые и красные интервалы проверены, включая границу цикла. Исходные пешеходные и защитные минимумы сохранены. Эти ограничения относятся к модели, а не к сертифицированному плану реального контроллера.</p>
    <h2>Сравнение на одинаковом спросе и seed {result['seed']}</h2><table><tr><th>Показатель</th><th>Исходный</th><th>Подобранный</th></tr>{metrics}</table>
    <p>Один контрольный спрос; улучшение не гарантируется. Стоимость учитывает стоящие автомобили и ожидание въезда, интегрированные по фактическому времени SUMO. Пешеходный спрос сохраняется, но не входит в целевую функцию.</p>
    <h2>Полный цикл</h2><table><tr><th>Фаза</th><th>Сигналы SUMO</th><th>Тип</th><th>Было, с</th><th>Стало, с</th></tr>{phases}</table>
    <h2>Проверка интервалов по направлениям</h2><p>Направление — индекс управляемого соединения SUMO. Красный — непрерывный красный этого направления; «всем красный» — отдельная фаза цикла.</p>
    <table><tr><th>Направление</th><th>Цвет</th><th>Фактически, с</th><th>Разрешено, с</th><th>Соблюдено</th></tr>{ranges}</table>
    <p>В папке находятся scenario.sumocfg, network.net.xml, исходные маршруты, additional и results.json. Открытие: sumo-gui -c scenario.sumocfg.</p></html>'''


def optimize_model(config, output_dir, green_min=15, green_max=60, yellow_min=3, yellow_max=5,
                   red_min=10, red_max=90, all_red_min=1, all_red_max=3, cycle=None,
                   generations=3, population=8, seed=42, progress=None, cancel=None, backend='auto'):
    """Find a feasible plan, then improve its queue cost; never return invalid DNA."""
    limits = {'green': (green_min, green_max), 'yellow': (yellow_min, yellow_max),
              'red': (red_min, red_max), 'all_red': (all_red_min, all_red_max)}
    for label, (lo,hi) in limits.items():
        if not all(math.isfinite(float(v)) for v in (lo,hi)) or lo < 0 or (lo == 0 and label != 'all_red') or hi < lo:
            raise ValueError(f'{label}: проверьте неотрицательные границы (зелёный, жёлтый, красный должны быть положительными)')
    if generations < 1 or population < 2: raise ValueError('Нужно минимум одно поколение и две особи')
    if not _SIMULATION_LOCK.acquire(blocking=False): raise ValueError('Другой расчёт светофора уже выполняется')
    progress = progress or (lambda message: None)
    try:
        config_root=ET.parse(config).getroot()
        step_entry=config_root.find('time/step-length')
        step=float(step_entry.get('value')) if step_entry is not None else 1.
        problem = _problem(_net_path(config), limits, cycle, step)
        engine, engine_name = _engine(backend)
        output = Path(output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)
        source_hash = hashlib.sha256(_net_path(config).read_bytes()).hexdigest()
        rng = random.Random(seed)
        def vertex():
            found = _solve_plan(problem,[rng.uniform(-1,1) for _ in problem['states']])
            if found is None: raise ValueError('Не удалось построить допустимый план')
            return found
        history, cache = [], {}
        with tempfile.TemporaryDirectory(prefix='trafficai_signals_') as temp:
            staged = _stage_scenario(config, temp)
            temp_net = _net_path(staged)
            def evaluate(values):
                key = tuple(round(v,7) for v in values)
                if key not in cache:
                    _write_network(problem, values, temp_net)
                    cache[key] = _evaluate(staged, engine, seed, cancel)
                return cache[key]
            progress('Оценка исходного плана…')
            baseline = evaluate(problem['baseline'])
            candidates = [problem['feasible']] + [vertex() for _ in range(population-1)]
            if validate_durations(problem, problem['baseline']): candidates[0] = problem['baseline']
            best, best_metrics = None, None
            for gen in range(generations):
                ranked = []
                for index, values in enumerate(candidates):
                    if cancel is not None and cancel.is_set(): raise InterruptedError('Расчёт отменён')
                    if not validate_durations(problem, values): raise RuntimeError('Внутренняя ошибка ограничений плана')
                    progress(f'Поколение {gen+1}/{generations}, вариант {index+1}/{population}')
                    metrics = evaluate(values)
                    ranked.append((metrics['waiting_vehicle_seconds'], values, metrics))
                ranked.sort(key=lambda item:item[0])
                if best_metrics is None or ranked[0][0] < best_metrics['waiting_vehicle_seconds']:
                    _,best,best_metrics = ranked[0]
                history.append(best_metrics['waiting_vehicle_seconds'])
                if gen + 1 == generations:
                    break
                elites = [item[1] for item in ranked[:max(1,population//3)]]
                candidates = [best]
                while len(candidates) < population:
                    a,b = rng.choice(elites), vertex()
                    alpha = rng.random()
                    mixed=[alpha*x+(1-alpha)*y for x,y in zip(a,b)]
                    child=_solve_plan(problem,[0.]*len(mixed),target=mixed)
                    # A solver numerical failure is not a reason to export an
                    # invalid plan; retain the already validated parent.
                    candidates.append(child if child is not None else list(a))
        if not validate_durations(problem, best): raise RuntimeError('Итоговый план не прошёл проверку ограничений')
        final_config = _stage_scenario(config, output)
        _write_network(problem, best, _net_path(final_config))
        checked = [dict(run, duration=sum(best[i] for i in run['indices'])) for run in problem['runs']]
        result = {'source_config': str(Path(config).resolve()), 'source_network_sha256': source_hash,
                  'method': 'constrained genetic search', 'backend': engine_name, 'seed': seed,
                  'cycle': problem['cycle'], 'step_seconds': step, 'limits': limits, 'baseline': baseline, 'optimized': best_metrics,
                  'history': history, 'phases': [{'state':s,'kind':k,'before':a,'after':b} for s,k,a,b in zip(problem['states'],problem['kinds'],problem['baseline'],best)],
                  'checked_intervals': checked, 'constraints_satisfied': True,
                  'config_file': str(final_config), 'report_file': str(output/'report.html')}
        (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        (output/'report.html').write_text(_report(result),encoding='utf-8')
        progress('Готово: все ограничения соблюдены')
        return result
    finally:
        _SIMULATION_LOCK.release()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path('sumo/intersection.sumocfg'))
    parser.add_argument('--output',type=Path,default=Path('runs/signals'))
    parser.add_argument('--prepare-controlled',action='store_true')
    for color,lo,hi in [('green',15,60),('yellow',3,5),('red',10,90),('all-red',1,3)]:
        parser.add_argument('--'+color+'-min',type=float,default=lo)
        parser.add_argument('--'+color+'-max',type=float,default=hi)
    parser.add_argument('--cycle',type=float)
    parser.add_argument('--generations',type=int,default=3)
    parser.add_argument('--population',type=int,default=8)
    parser.add_argument('--seed',type=int,default=42)
    parser.add_argument('--backend',choices=['auto','traci','libsumo'],default='auto')
    args=vars(parser.parse_args())
    config,output=args.pop('config'),args.pop('output')
    if args.pop('prepare_controlled'):
        config=prepare_controlled_model(config,output/'prepared')
    try:
        result=optimize_model(config,output/'optimized',**args,progress=lambda m: print(m,flush=True))
    except (ValueError,ImportError) as exc: parser.error(str(exc))
    print(result['report_file'])


if __name__=='__main__': main()
