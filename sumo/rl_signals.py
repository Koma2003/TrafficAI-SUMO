"""Experimental tabular Q-learning for one vehicle-only SUMO intersection.

Learns bounded green/yellow/all-red durations. Existing conflict-free green
states are preserved. Pedestrian networks are rejected by this demo controller.
"""
import argparse
import hashlib
import html
import json
import math
import random
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import traci

POLICY_SCHEMA = 2
CONTROLLER_VERSION = '2.0'
STATE_ENCODING = 'phase-index:sorted-controlled-lanes:halting-floor-div-3-capped-at-3'
METRICS = ('queue_and_insertion_wait_vehicle_seconds', 'arrived', 'maximum_queue_vehicles')


def duration_choices(minimum, maximum, step):
    if not all(math.isfinite(v) for v in (minimum, maximum, step)) or minimum <= 0 or maximum < minimum or step <= 0:
        raise ValueError('Требуется 0 < минимум <= максимум и положительный шаг')
    count = int((maximum-minimum)/step)
    if count > 50:
        raise ValueError('Слишком много вариантов: увеличьте шаг длительности')
    return sorted(set([minimum + i*step for i in range(count+1)] + [maximum]))


def load_program(config):
    """Accept a simple cyclic G → yellow → optional all-red program only."""
    config = Path(config).resolve()
    root = ET.parse(config).getroot()
    entry = root.find('input/net-file')
    if entry is None: raise ValueError('В конфиге нет net-file')
    net = (config.parent / entry.get('value')).resolve()
    network = ET.parse(net).getroot()
    if any(e.get('function') in {'crossing', 'walkingarea'} for e in network.findall('edge')):
        raise ValueError('RL-обучение поддерживает только автоперекрёсток без пешеходов. Используйте examples/signal_demo.')
    programs = network.findall('tlLogic')
    if len(programs) != 1: raise ValueError('Для RL-примера нужна сеть ровно с одним светофором')
    program = programs[0]
    if program.get('type', 'static') != 'static':
        raise ValueError('RL-пример поддерживает только исходную статическую программу')
    phases = program.findall('phase')
    if not phases:
        raise ValueError('В программе светофора нет фаз')
    width = len(phases[0].get('state', ''))
    kinds = []
    for phase in phases:
        state = phase.get('state', '')
        duration = float(phase.get('duration', 'nan'))
        if len(state) != width or width == 0 or not math.isfinite(duration) or duration <= 0:
            raise ValueError('Некорректное состояние или длительность исходной фазы')
        if phase.get('next') is not None:
            raise ValueError('Ветвящиеся переходы next не поддерживаются этим RL-примером')
        if set(state) <= {'r'}:
            kinds.append('red')
        elif 'y' in state and set(state) <= {'r', 'y'}:
            kinds.append('yellow')
        elif 'G' in state and set(state) <= {'r', 'g', 'G'}:
            kinds.append('green')
        else:
            raise ValueError('Сложные/совмещённые переходные фазы не поддерживаются этим RL-примером')
    greens = []
    for i, phase in enumerate(phases):
        if kinds[i] != 'green':
            continue
        state = phase.get('state')
        j = (i + 1) % len(phases)
        if kinds[j] != 'yellow':
            raise ValueError('После каждой зелёной фазы должна следовать одна жёлтая')
        following = phases[j]
        yellow = following.get('state')
        expected_yellow = ''.join('y' if c in 'gG' else 'r' for c in state)
        if yellow != expected_yellow:
            raise ValueError('Поддерживается только полный жёлтый переход для завершившихся зелёных направлений')
        all_red = 0.0
        j = (j + 1) % len(phases)
        while kinds[j] == 'red':
            all_red += float(phases[j].get('duration'))
            j = (j + 1) % len(phases)
        if kinds[j] != 'green':
            raise ValueError('После жёлтого/всем красный должна следовать зелёная фаза')
        greens.append({'green': state, 'yellow': yellow, 'green_seconds': float(phase.get('duration')),
                       'yellow_seconds': float(following.get('duration')), 'all_red_seconds': all_red})
    if not greens: raise ValueError('Не найдено автомобильных зелёных фаз')
    return programs[0].get('id'), greens


def valid_action_indices(actions, phase):
    """Original clearances are hard lower bounds for each transition."""
    indices = [i for i, (_, yellow, red) in enumerate(actions)
               if yellow + 1e-9 >= phase['yellow_seconds']
               and red + 1e-9 >= phase.get('all_red_seconds', 0)]
    if not indices:
        raise ValueError('Заданные диапазоны не позволяют сохранить исходный жёлтый/всем красный')
    return indices


def choose_action(scores, counts, allowed, actions):
    """Do not treat untried optimistic zero values as a trained optimum."""
    tried = [i for i in allowed if counts[i] > 0]
    if tried:
        return max(tried, key=lambda i: scores[i]), False
    return min(allowed, key=lambda i: tuple(actions[i])), True


def policy_context(config, tls, phases, bounds, green_step, horizon):
    """Portable fingerprint of input contents, not installation directories."""
    config = Path(config).resolve()
    root = ET.parse(config).getroot()
    inputs = [{'kind': 'configuration', 'sha256': hashlib.sha256(config.read_bytes()).hexdigest()}]
    for kind in ('net-file', 'route-files', 'additional-files'):
        entry = root.find('input/' + kind)
        if entry is not None:
            for name in entry.get('value', '').split(','):
                if name.strip():
                    path = config.parent / name.strip()
                    inputs.append({'kind': kind, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
                    if kind == 'additional-files' and ET.parse(path).getroot().find('.//tlLogic') is not None:
                        raise ValueError('Программы tlLogic в additional-файлах пока не поддерживаются')
    fingerprint = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return {'policy_schema': POLICY_SCHEMA, 'controller_version': CONTROLLER_VERSION,
            'input_fingerprint': fingerprint, 'inputs': inputs, 'tls': tls,
            'phases': [{k: p[k] for k in ('green', 'yellow', 'green_seconds', 'yellow_seconds', 'all_red_seconds')} for p in phases],
            'state_encoding': STATE_ENCODING, 'bounds': bounds, 'green_step_seconds': green_step,
            'horizon_seconds': horizon}


def validate_policy_context(saved, expected):
    if not isinstance(saved, dict):
        raise ValueError('У политики нет контекста: старую политику необходимо обучить заново')
    for key, value in expected.items():
        if saved.get(key) != value:
            raise ValueError('Политика не соответствует текущему сценарию/настройкам: ' + key)


def validate_policy_table(saved, actions):
    if saved.get('actions') != [list(a) for a in actions]:
        raise ValueError('Диапазоны действий отличаются от сохранённой политики')
    q, visits = saved.get('q'), saved.get('visits')
    if not isinstance(q, dict) or not isinstance(visits, dict) or set(q) != set(visits):
        raise ValueError('Некорректные таблицы Q/посещений')
    for key, scores in q.items():
        counts = visits[key]
        if (not isinstance(scores, list) or len(scores) != len(actions)
                or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in scores)
                or not isinstance(counts, list) or len(counts) != len(actions)
                or not all(isinstance(v, int) and v >= 0 for v in counts)):
            raise ValueError('Некорректные таблицы Q/посещений')
    return q, visits


def run_episode(config, tls, phases, actions, q, rng, seed, horizon, training=False, epsilon=0, gui=False,
                visits=None, expected_runtime=None):
    """Integrate queue cost in real simulation seconds, including insertion wait."""
    traci.start(['sumo-gui' if gui else 'sumo', '-c', str(Path(config).resolve()), '--seed', str(seed),
                 '--start', '--quit-on-end',
                 '--no-step-log', 'true', '--duration-log.disable', 'true', '--time-to-teleport', '-1'])
    elapsed = 0.0
    cost = 0.0
    arrived = 0
    maximum_queue = 0
    used = []
    fallback_decisions = 0
    if visits is None:
        visits = {key: [int(v != 0) for v in values] for key, values in q.items()}
    try:
        lanes = sorted(set(traci.trafficlight.getControlledLanes(tls)))
        delta = float(traci.simulation.getDeltaT())
        started = float(traci.simulation.getTime())
        runtime = {'simulation_step_seconds': delta, 'state_layout': lanes,
                   'sumo_version': list(traci.getVersion())}
        if not math.isfinite(delta) or delta <= 0:
            raise ValueError('Некорректный шаг времени SUMO')
        if expected_runtime:
            validate_policy_context(expected_runtime, runtime)
        allowed_per_phase = [valid_action_indices(actions, p) for p in phases] if actions else None
        if not actions:
            for phase in phases:
                valid_action_indices([phase['baseline']], phase)
        durations = [v for action in actions for v in action] if actions else [v for p in phases for v in p['baseline']]
        for seconds in [horizon, *durations]:
            if not math.isfinite(seconds) or seconds <= 0 or not math.isclose(seconds / delta, round(seconds / delta), abs_tol=1e-7):
                raise ValueError('Длительности фаз и горизонт должны быть положительными кратными шагу SUMO')
        def state(index):
            return str(index) + ':' + ','.join(str(min(3, traci.lane.getLastStepHaltingNumber(lane)//3)) for lane in lanes)
        def advance(signal, seconds):
            nonlocal elapsed, cost, arrived, maximum_queue
            if elapsed >= horizon - 1e-8:
                return 0.0
            traci.trafficlight.setRedYellowGreenState(tls, signal)
            subtotal = 0.0
            target = started + min(horizon, elapsed + seconds)
            while float(traci.simulation.getTime()) < target - 1e-8:
                before = float(traci.simulation.getTime())
                traci.simulationStep()
                now = float(traci.simulation.getTime())
                dt = now - before
                if dt <= 0 or now > target + 1e-7:
                    raise ValueError('SUMO не соблюдает ожидаемый шаг/горизонт времени')
                elapsed = now - started
                queue = sum(traci.lane.getLastStepHaltingNumber(lane) for lane in lanes)
                pending = len(traci.simulation.getPendingVehicles())
                subtotal += (queue + pending) * dt
                arrived += traci.simulation.getArrivedNumber()
                maximum_queue = max(maximum_queue, queue)
            cost += subtotal
            return subtotal
        phase_index = 0
        while elapsed < horizon - 1e-8:
            phase = phases[phase_index]
            key = state(phase_index)
            if actions:
                scores = q.setdefault(key, [0.0] * len(actions))
                counts = visits.setdefault(key, [0] * len(actions))
                allowed = allowed_per_phase[phase_index]
                if training and rng.random() < epsilon:
                    action_index, fallback = rng.choice(allowed), False
                else:
                    action_index, fallback = choose_action(scores, counts, allowed, actions)
                fallback_decisions += int(fallback)
                green, yellow, red = actions[action_index]
            else:
                action_index = 0
                green, yellow, red = phase['baseline']
            start = elapsed
            penalty = advance(phase['green'], green)
            penalty += advance(phase['yellow'], yellow)
            penalty += advance('r' * len(phase['green']), red)
            used.append({'phase': phase_index, 'green': green, 'yellow': yellow, 'all_red': red,
                         'start_seconds': start, 'actual_interval_seconds': elapsed - start})
            next_index = (phase_index + 1) % len(phases)
            if training:
                future = 0.0
                if elapsed < horizon - 1e-8:
                    next_key = state(next_index)
                    next_scores = q.setdefault(next_key, [0.0] * len(actions))
                    next_counts = visits.setdefault(next_key, [0] * len(actions))
                    tried = [i for i in allowed_per_phase[next_index] if next_counts[i] > 0]
                    if tried:
                        future = max(next_scores[i] for i in tried)
                old = q[key][action_index]
                # Semi-Markov discount: actions last different numbers of seconds.
                q[key][action_index] = old + .2 * (-penalty + .99**(elapsed-start)*future - old)
                visits[key][action_index] += 1
            phase_index = next_index
    finally:
        traci.close()
    return {'queue_and_insertion_wait_vehicle_seconds': cost, 'arrived': arrived,
            'maximum_queue_vehicles': maximum_queue, 'decisions': used,
            'fallback_decisions': fallback_decisions, 'elapsed_seconds': elapsed, 'runtime': runtime}


def save_comparison(result, output):
    """Neutral held-out comparison, including a simple short fixed plan."""
    rows = []
    for key, label in [('queue_and_insertion_wait_vehicle_seconds','Очередь + ожидание въезда, авто·с'),
                       ('arrived','Прибывшие автомобили'),('maximum_queue_vehicles','Максимальная очередь, авто')]:
        cells = ''.join(f'<td>{result["summary"][method][key]["mean"]:.2f}</td>' for method in ('baseline', 'minimum_fixed', 'rl'))
        rows.append(f'<tr><td>{label}</td>{cells}</tr>')
    seeds = ''.join('<tr><td>' + str(item['seed']) + '</td>' + ''.join(
        f'<td>{item[method][METRICS[0]]:.2f}</td>' for method in ('baseline', 'minimum_fixed', 'rl')) + '</tr>'
        for item in result['evaluations'])
    decisions = ''.join(f'<tr><td>{d["phase"]}</td><td>{d["green"]}</td><td>{d["yellow"]}</td><td>{d["all_red"]}</td></tr>' for d in result['rl']['decisions'])
    text = f'''<!doctype html><html lang="ru"><meta charset="utf-8"><title>TrafficAI: RL-пример</title>
    <style>body{{font:17px system-ui;max-width:1000px;margin:40px auto;padding:20px}}table{{border-collapse:collapse;width:100%;margin:20px 0}}td,th{{border:1px solid #ccd5df;padding:10px;text-align:left}}</style>
    <h1>RL-обучение светофора</h1><p>Q-learning. Эпизодов: {len(result['history'])}. Seed обучения: {result['seed']}; контрольные seeds: {[e['seed'] for e in result['evaluations']]}.</p>
    <p>Автомобильный перекрёсток, без пешеходов. Для каждого seed спрос и условия трёх контроллеров одинаковы. RL не гарантирует улучшения, в том числе относительно простого фиксированного плана. Эти результаты не подтверждают применимость к реальной сети.</p>
    <p>Диапазоны (секунды): {html.escape(str(result['bounds']))}. Red означает «всем красный», а не красный отдельного направления.</p>
    <h2>Средние показатели контрольных прогонов</h2><table><tr><th>Показатель</th><th>Исходный, приведённый к ограничениям</th><th>Простой минимальный план</th><th>RL</th></tr>{''.join(rows)}</table>
    <p>Стоимость — интеграл числа остановившихся автомобилей на подходах и ожидающих въезда по времени симуляции. Это не среднее время поездки. Для первого плана исходные зелёные приведены к диапазонам, для второго выбраны минимальные допустимые длительности. Исходные жёлтые и «всем красный» никогда не сокращаются.</p>
    <h2>Стоимость по контрольным seeds, авто·с</h2><table><tr><th>Seed</th><th>Исходный с ограничениями</th><th>Минимальный фиксированный</th><th>RL</th></tr>{seeds}</table>
    <h2>Решения RL в первом контрольном прогоне</h2><table><tr><th>Фаза</th><th>Зелёный, с</th><th>Жёлтый, с</th><th>Всем красный, с</th></tr>{decisions}</table>
    <p>Неизвестное состояние использует явный минимальный фиксированный fallback. Непроверенные нулевые значения Q не считаются обученным оптимумом. Число fallback-решений сохранено в JSON. Последний интервал может быть обрезан концом моделирования.</p>
    <p>Результаты и политика: results.json, policy.json. Расчёт выполнен в симуляторе, без проверки пешеходного клиренса и применимости к реальному объекту.</p></html>'''
    (Path(output)/'report.html').write_text(text, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=['traci', 'libsumo'], default='traci')
    parser.add_argument('--play-policy', type=Path, help='Replay a previously trained policy without retraining')
    parser.add_argument('--gui', action='store_true', help='Display policy replay in sumo-gui (TraCI only)')
    parser.add_argument('--config', type=Path, default=Path('examples/signal_demo/demo.sumocfg'))
    for color, lower, upper in [('green',15,45), ('yellow',3,5), ('red',2,4)]:
        parser.add_argument('--'+color+'-min', type=int, default=lower)
        parser.add_argument('--'+color+'-max', type=int, default=upper)
    parser.add_argument('--green-step', type=int, default=10)
    parser.add_argument('--episodes', type=int, default=10)
    parser.add_argument('--evaluation-seeds', type=int, default=3, help='Number of held-out demand seeds, 1–20')
    parser.add_argument('--duration', type=int, default=360)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--output', type=Path, default=Path('runs/rl'))
    args = parser.parse_args()
    global traci
    if args.backend == 'libsumo':
        try:
            import libsumo as traci
        except ImportError as exc:
            parser.error('libsumo недоступен: ' + str(exc))
    if args.episodes < 1 or args.duration < 1: parser.error('Число эпизодов и длительность должны быть положительными')
    if not 1 <= args.evaluation_seeds <= 20: parser.error('Контрольных seeds должно быть от 1 до 20')
    try:
        green = duration_choices(args.green_min, args.green_max, args.green_step)
        yellow = duration_choices(args.yellow_min, args.yellow_max, 1)
        red = duration_choices(args.red_min, args.red_max, 1)
        tls, phases = load_program(args.config)
    except (ValueError, OSError, ET.ParseError) as exc: parser.error(str(exc))
    actions = [(g,y,r) for g in green for y in yellow for r in red]
    if len(actions)>500: parser.error('Более 500 действий: уменьшите диапазоны или увеличьте шаг')
    bounds = {c: [getattr(args, c + '_min'), getattr(args, c + '_max')] for c in ('green', 'yellow', 'red')}
    try:
        for phase in phases:
            allowed = valid_action_indices(actions, phase)
            minimum = min((actions[i] for i in allowed), key=tuple)
            phase['minimum_plan'] = list(minimum)
            phase['baseline'] = [max(args.green_min, min(args.green_max, phase['green_seconds'])), minimum[1], minimum[2]]
        context = policy_context(args.config, tls, phases, bounds, args.green_step, args.duration)
    except (ValueError, OSError, ET.ParseError) as exc:
        parser.error(str(exc))
    q = {}
    visits = {}
    rng = random.Random(args.seed)
    history = []
    if args.play_policy:
        if args.backend != 'traci' and args.gui: parser.error('--gui требует --backend traci')
        try:
            saved = json.loads(args.play_policy.read_text(encoding='utf-8'))
            validate_policy_context(saved.get('context'), context)
            q, visits = validate_policy_table(saved, actions)
            metrics = run_episode(args.config,tls,phases,actions,q,rng,args.seed+100000,args.duration,
                                  gui=args.gui, visits=visits, expected_runtime=saved['context'])
        except (ValueError, OSError, KeyError, TypeError) as exc:
            parser.error(str(exc))
        args.output.mkdir(parents=True,exist_ok=True)
        (args.output/'replay.json').write_text(json.dumps(metrics,indent=2),encoding='utf-8')
        print('Воспроизведение завершено:', args.output.resolve())
        return
    for episode in range(args.episodes):
        metrics = run_episode(args.config,tls,phases,actions,q,rng,args.seed+episode,args.duration,
                              True,max(.1,1-episode/args.episodes),visits=visits)
        if episode == 0:
            context.update(metrics['runtime'])
        else:
            validate_policy_context(context, metrics['runtime'])
        history.append({k:metrics[k] for k in METRICS})
        print(f'Эпизод {episode+1}/{args.episodes}: {history[-1]}', flush=True)
    evaluation_seed = args.seed + 100000
    minimum_phases = [dict(phase, baseline=phase['minimum_plan']) for phase in phases]
    evaluations = []
    for seed in range(evaluation_seed, evaluation_seed + args.evaluation_seeds):
        baseline = run_episode(args.config,tls,phases,None,{},rng,seed,args.duration,expected_runtime=context)
        minimum = run_episode(args.config,tls,minimum_phases,None,{},rng,seed,args.duration,expected_runtime=context)
        learned = run_episode(args.config,tls,phases,actions,q,rng,seed,args.duration,visits=visits,expected_runtime=context)
        evaluations.append({'seed': seed, 'baseline': baseline, 'minimum_fixed': minimum, 'rl': learned})
    summary = {}
    for method in ('baseline', 'minimum_fixed', 'rl'):
        summary[method] = {}
        for metric in METRICS:
            values = [item[method][metric] for item in evaluations]
            summary[method][metric] = {'mean': sum(values) / len(values), 'min': min(values), 'max': max(values)}
    args.output.mkdir(parents=True,exist_ok=True)
    result = {'method':'tabular Q-learning (experimental)', 'backend':args.backend,'seed':args.seed,'evaluation_seed':evaluation_seed,
              'bounds': bounds, 'context': context,
              'red_means':'all-red clearance, not the red time of an individual direction',
              'baseline_description':'Original greens clipped to chosen bounds; minimum permitted yellow/all-red preserving original clearances. Same demand and SUMO seed.',
              'history':history,'baseline':evaluations[0]['baseline'],'minimum_fixed':evaluations[0]['minimum_fixed'],
              'rl':evaluations[0]['rl'], 'evaluations':evaluations, 'summary':summary,
              'limitations':'One vehicle-only TLS; educational Q-learning, no guaranteed improvement; no field deployment or pedestrian clearance validation.'}
    (args.output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    (args.output/'policy.json').write_text(json.dumps({'context':context,'actions':actions,'q':q,'visits':visits},indent=2),encoding='utf-8')
    save_comparison(result, args.output)
    print('Сравнение и политика:', args.output.resolve(), flush=True)


if __name__ == '__main__': main()
